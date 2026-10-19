"""Bounded ingestion of an explicitly supplied historical evidence ZIP.

Attached files are data only. No member is executed and original bytes remain
untouched. Failed destinations are retained, never reused or overwritten.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from . import archive, historical

EXPECTED_SHA256 = '02bbc0f878667fb71f3cbf07b3367c648e152889ff27c3047050fd831744ab07'
EXPECTED_BINARY_SHA256 = 'a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1'
MAX_MEMBERS = 10000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RATIO = 1000


def _identity(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    while chunk := handle.read(archive.BLOCK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def ingest_archive(zip_path, destination, expected_sha256=EXPECTED_SHA256):
    """Return a JSON-serializable report; create destination exclusively, mode 0700.

    ZIP timestamps have no timezone: original DOS tuples are preserved in the
    manifest; extracted mtimes use those wall-clock tuples interpreted as UTC.
    """
    if not Path(zip_path).is_absolute() or not Path(destination).is_absolute():
        raise ValueError('explicit absolute source and destination paths required')
    source, dest = Path(zip_path).absolute(), Path(destination).absolute()
    if not source.is_absolute() or any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError('ZIP source must be an explicit regular local path without symlinks')
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TOTAL_BYTES:
        raise ValueError('invalid or oversized ZIP source')
    if any(p.is_symlink() for p in (dest, *dest.parents)):
        raise ValueError('symlink destination')
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        if _signature(os.fstat(handle.fileno())) != _signature(before):
            raise ValueError('ZIP source changed before open')
        sha_before = _identity(handle)
        if sha_before != expected_sha256:
            raise ValueError('ZIP SHA256 mismatch')
        with zipfile.ZipFile(handle) as z:
            entries, seen, total = z.infolist(), set(), 0
            if not entries or len(entries) > MAX_MEMBERS:
                raise ValueError('ZIP member count outside bounds')
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                clean = name.rstrip('/')
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (not clean or '\\' in name or '\x00' in entry.orig_filename or name.startswith('/')
                        or ':' in name or any(p in ('', '.', '..') for p in clean.split('/'))
                        or parts[:2] != ('results', 'conference_c03')
                        or clean.casefold() in seen):
                    raise ValueError(f'unsafe or colliding ZIP member: {name!r}')
                seen.add(clean.casefold())
                if kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
                    raise ValueError('ZIP symlink or special member')
                if entry.flag_bits & 1:
                    raise ValueError('encrypted ZIP member')
                if entry.is_dir() and entry.file_size:
                    raise ValueError('directory ZIP member contains data')
                total += entry.file_size
                if (entry.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES
                        or entry.file_size / max(1, entry.compress_size) > MAX_RATIO):
                    raise ValueError('ZIP expansion bounds exceeded')
                if not entry.is_dir() and Path(name).suffix.lower() in archive.DISALLOWED_SUFFIXES:
                    raise ValueError('binary/model ZIP member')
            # Reject file/directory prefix ambiguity before creating anything.
            files = {e.filename.casefold() for e in entries if not e.is_dir()}
            if any('/'.join(e.filename.casefold().split('/')[:i]) in files
                   for e in entries for i in range(1, len(e.filename.rstrip('/').split('/')))):
                raise ValueError('file/directory ZIP collision')
            dest.mkdir(mode=0o700, parents=False, exist_ok=False)
            manifest = []
            for entry in entries:
                target = dest / entry.filename
                metadata = {'path': entry.filename, 'bytes': entry.file_size,
                            'compressed_bytes': entry.compress_size, 'crc32': entry.CRC,
                            'date_time': list(entry.date_time), 'external_attr': entry.external_attr,
                            'internal_attr': entry.internal_attr, 'create_system': entry.create_system,
                            'create_version': entry.create_version, 'extract_version': entry.extract_version,
                            'flag_bits': entry.flag_bits, 'compress_type': entry.compress_type,
                            'header_offset': entry.header_offset, 'extra_hex': entry.extra.hex(),
                            'comment_hex': entry.comment.hex(), 'directory': entry.is_dir()}
                if entry.is_dir():
                    with z.open(entry) as reader:
                        if reader.read(1):
                            raise ValueError('directory ZIP member contains data')
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    metadata['sha256'] = hashlib.sha256(b'').hexdigest()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    digest, size = hashlib.sha256(), 0
                    with z.open(entry) as reader, target.open('xb') as writer:
                        os.chmod(target, 0o600)
                        while chunk := reader.read(archive.BLOCK_SIZE):
                            if (size == 0 and not archive._allowed(target, chunk)) or b'\x00' in chunk:
                                raise ValueError('binary/model content in ZIP')
                            size += len(chunk)
                            if size > entry.file_size or size > MAX_MEMBER_BYTES:
                                raise ValueError('ZIP expanded size mismatch')
                            digest.update(chunk)
                            writer.write(chunk)
                    if size != entry.file_size:
                        raise ValueError('ZIP member size mismatch')
                    metadata['sha256'] = digest.hexdigest()
                manifest.append(metadata)
            for entry in reversed(entries):
                stamp = calendar.timegm((*entry.date_time, 0, 0, 0))
                os.utime(dest / entry.filename, (stamp, stamp), follow_symlinks=False)
        sha_after = _identity(handle)
        if (sha_after != sha_before or _signature(os.fstat(handle.fileno())) != _signature(before)
                or _signature(source.lstat()) != _signature(before)):
            raise ValueError('ZIP source changed during ingestion')
    report = {'status': 'PASS', 'root': str(dest / 'results/conference_c03'),
              'destination': str(dest), 'source': str(source), 'source_sha256_before': sha_before,
              'source_sha256_after': sha_after, 'source_stat': list(_signature(before)),
              'compressed_zip_bytes': before.st_size, 'crc_verified': True,
              'timestamp_interpretation': 'Original ZIP wall-clock tuple retained; filesystem mtime interpreted as UTC',
              'members': manifest}
    archive._json(dest / 'ZIP_INGESTION_REPORT.json', report)
    return report


def validate_recovered(ingestion_report, plan, collection_destination=None):
    """One gate for complete original C03 census and historical identity."""
    root = Path(ingestion_report['root'])
    for member in ingestion_report['members']:
        if not member['directory']:
            identity, _ = archive._stream_identity(Path(ingestion_report['destination']) / member['path'])
            if identity['sha256'] != member['sha256'] or identity['bytes'] != member['bytes']:
                raise ValueError('Recovered member changed since ZIP ingestion')
    output = (Path(collection_destination) if collection_destination is not None
              else Path(ingestion_report['destination']) / 'verification')
    # Empty isolated repository prevents accidental inclusion of another C03 tree.
    isolated = Path(ingestion_report['destination']) / 'empty_repository'
    isolated.mkdir(mode=0o700)
    collected = archive.collect_history(isolated, output, historical_dirs=[root], include_repo_defaults=False)
    anchor = historical.check_anchor(output, plan)
    errors = list(anchor['errors'])
    runs = collected['runs']
    expected = {(str(r), arm) for r in range(1, 7) for arm in ('BIG_ONLY', 'ALL_CORES')}
    if len(runs) != 12 or {(str(r['round']), r['arm']) for r in runs} != expected:
        errors.append('Expected twelve unique round/arm references (rounds 1..6, BIG_ONLY/ALL_CORES)')
    for role in ('run', 'detector', 'phase', 'server', 'environment'):
        if sum(r['roles'][role]['status'] == 'present' for r in runs) != 12:
            errors.append(f'Incomplete recovered role: {role}')
        if role != 'environment' and collected['collected_unique_file_counts_by_role'][role] != 12:
            errors.append(f'Expected twelve unique recovered files for role: {role}')
    if collected['collected_unique_file_counts_by_role']['environment'] != 2 or len(anchor['records']) != 2:
        errors.append('Expected two unique original environment records')
    for record in anchor['records']:
        if record['llama_cpp'].get('diagnostic_server_binary', {}).get('sha256') != EXPECTED_BINARY_SHA256:
            errors.append('Historical binary SHA256 mismatch')
    result = {'status': 'PASS' if not errors and ingestion_report['status'] == 'PASS' else 'NOT_READY',
              'errors': errors, 'anchor': anchor, 'history': collected,
              'original_zip_sha256': ingestion_report['source_sha256_before']}
    archive._json(Path(ingestion_report['destination']) / 'RECOVERED_VERIFICATION_REPORT.json', result)
    return result
