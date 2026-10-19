"""Bounded, byte-preserving private evidence collection (no experiment execution)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import stat
import zipfile
import zlib

BLOCK_SIZE = 1024 * 1024
DISALLOWED_SUFFIXES = {'.gguf', '.bin', '.so', '.a', '.o', '.exe', '.pyc', '.zip'}

PRIVATE_NOTICE = "PRIVATE evidence archive: originals are preserved; public redaction requires a separate copy.\n"
VALIDATION_METHOD = """# Private archive validation method

Source bytes and nanosecond mtimes are snapshotted before ZIP creation and
checked again before each member is written and after the ZIP is closed.
ARCHIVE_MANIFEST.json records each evidence member's SHA256, size and mtime.
This note records the method, not a claim that a particular archive passed.

After closure, validate_archive checks safe and unique member names, a complete
manifest/member census, ZIP CRCs, and every evidence member's SHA256 and size.
It calculates the archive SHA256 from the finished ZIP. That archive hash cannot
be stored inside the same ZIP without changing it. archive_session writes a
sidecar and verifies it before returning PASS; a recipient may send just the ZIP.
Standalone validation checks all internal integrity data and reports whether an
external expected hash or sidecar was available and matched. Without one, PASS
establishes internal integrity, not independent authentication of the archive.
"""
TEXT_SUFFIXES = {'.json', '.jsonl', '.csv', '.tsv', '.txt', '.md', '.log', '.env', '.yaml', '.yml', '.toml', '.py', '.sh', '.patch', '.diff', '.c', '.h'}
RELATED_METADATA = (
    'conference/diagnostic/c03_build_manifest.json',
    'results/conference_c03_build/manifest.json',
    'conference/diagnostic/llama_cpp_phase_mark.patch',
    'llama.cpp/build-diag/CMakeCache.txt',
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _safe_name(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and '..' not in p.parts and '\\' not in name


def _snapshot(path):
    """Open without following links; fail if identity/content changes while reading."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'not a regular file: {path}')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        data = handle.read()
        after = os.fstat(handle.fileno())
    current = path.lstat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len({signature(s) for s in (before, opened, after, current)}) != 1:
        raise ValueError(f'source mutated: {path}')
    return data, before.st_mtime_ns


def _allowed(path, data):
    return path.suffix.lower() not in DISALLOWED_SUFFIXES and not data.startswith((b'\x7fELF', b'MZ', b'GGUF'))


def _stream_identity(path, sink=None):
    """Hash a regular file in bounded chunks, optionally copying those same bytes."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'not a regular file: {path}')
    digest, size, prefix = hashlib.sha256(), 0, b''
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        while chunk := handle.read(BLOCK_SIZE):
            if size == 0:
                prefix = chunk[:4]
            digest.update(chunk)
            size += len(chunk)
            if sink is not None:
                sink.write(chunk)
        after = os.fstat(handle.fileno())
    current = path.lstat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len({signature(s) for s in (before, opened, after, current)}) != 1:
        raise ValueError(f'source mutated: {path}')
    return {'sha256': digest.hexdigest(), 'bytes': size, 'mtime_ns': before.st_mtime_ns}, prefix


def _binary_signature(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        return not _allowed(path, handle.read(4))


def _walk(root):
    if root.is_symlink():
        raise ValueError(f'symlink root: {root}')
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if not path.is_symlink() and path.is_file():
                yield path


def _exclusive(path, data, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f'symlink destination: {path}')
    with path.open('xb') as handle:
        handle.write(data)
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime), follow_symlinks=False)


def _json(path, value):
    _exclusive(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode())


def collect_history(repo_root: Path, output_dir: Path, historical_dirs=None, *, include_repo_defaults=True) -> dict:
    """Collect only C03 evidence roots and two explicitly enumerated metadata paths."""
    repo_root, output_dir = Path(repo_root).resolve(), Path(output_dir).absolute()
    roots = [repo_root / 'results/conference_c03'] if include_repo_defaults else []
    for candidate in historical_dirs or []:
        candidate = Path(candidate).absolute()
        if candidate.is_symlink() or candidate.name != 'conference_c03' or candidate.resolve() in (Path('/'), Path.home(), repo_root):
            raise ValueError(f'historical root must be an explicit conference_c03 directory: {candidate}')
        roots.append(candidate)
    roots = list(dict.fromkeys(roots))
    inventory, exclusions, runs, missing = [], [], [], []
    frozen = []
    for index, root in enumerate(roots):
        namespace = f'c03_{index:02d}'
        if root.is_symlink() or (root.exists() and root.resolve() != root.absolute()):
            raise ValueError(f'root traverses symlink: {root}')
        if output_dir.resolve().is_relative_to(root.resolve()):
            raise ValueError('output cannot be inside historical source')
        copied = set()
        for path in _walk(root):
            rel = path.relative_to(root).as_posix()
            # C03 root is not permission to collect arbitrary personal files.
            selected = rel in {'c03_runs.csv', 'signal_summary.csv', 'summary.md', 'summary.json', 'raw/plan.json'} or rel.startswith(('raw/runs/', 'raw/detector/', 'raw/phase_logs/', 'raw/server_logs/', 'raw/environment/', 'raw/failures/', 'preflight/', 'output/', 'failures/', 'build/'))
            if not selected or path.suffix.lower() not in TEXT_SUFFIXES or path.suffix.lower() in DISALLOWED_SUFFIXES:
                exclusions.append({'source': str(path), 'reason': 'not selected text evidence'})
                continue
            data, mtime = _snapshot(path)
            if not selected or path.suffix.lower() not in TEXT_SUFFIXES or b'\x00' in data or not _allowed(path, data):
                exclusions.append({'source': str(path), 'reason': 'not selected text evidence'})
                continue
            target = f'history/{namespace}/{rel}'
            _exclusive(output_dir / target, data, mtime)
            frozen.append((path, {'sha256': _sha(data), 'bytes': len(data), 'mtime_ns': mtime}))
            inventory.append({'path': target, 'source': str(path), 'sha256': _sha(data), 'bytes': len(data), 'mtime_ns': mtime})
            copied.add(rel)
        csv_path = root / 'c03_runs.csv'
        if 'c03_runs.csv' in copied:
            data, _ = _snapshot(csv_path)
            for row in csv.DictReader(io.StringIO(data.decode('utf-8-sig'))):
                stem = f"round_{int(row['round']):02d}_seq_{int(row['sequence_index']):02d}_{row['arm'].lower()}"
                roles = {'run': f'raw/runs/{stem}.json', 'detector': row.get('detector_file', ''), 'phase': row.get('phase_log', ''), 'server': row.get('server_log', ''), 'environment': row.get('environment_file', '')}
                status = {role: {'path': ref, 'status': 'present' if _safe_name(ref) and ref in copied else 'missing_or_unsafe'} for role, ref in roles.items()}
                runs.append({'namespace': namespace, 'round': row['round'], 'sequence_index': row['sequence_index'], 'arm': row['arm'], 'roles': status})
        else:
            missing.append({'namespace': namespace, 'role': 'run_reference_csv', 'path': 'c03_runs.csv'})
        for ref, role in [('raw/plan.json', 'original_plan'), ('preflight/environment.txt', 'preflight_environment'), ('preflight/c03_topology.env', 'preflight_topology'), ('preflight/selected_topology.json', 'selected_build_identity'), ('preflight/preflight_status.json', 'preflight_status')]:
            if ref not in copied:
                missing.append({'namespace': namespace, 'role': role, 'path': ref})
    for rel in RELATED_METADATA if include_repo_defaults else ():
        path = repo_root / rel
        if path.exists() and not any(p.is_symlink() for p in (path, *path.parents)):
            data, mtime = _snapshot(path)
            if _allowed(path, data):
                target = 'history/related/' + rel
                _exclusive(output_dir / target, data, mtime)
                frozen.append((path, {'sha256': _sha(data), 'bytes': len(data), 'mtime_ns': mtime}))
                inventory.append({'path': target, 'source': str(path), 'sha256': _sha(data), 'bytes': len(data), 'mtime_ns': mtime})
            else:
                missing.append({'namespace': 'related', 'role': 'build_metadata', 'path': rel, 'reason': 'excluded_binary'})
        else:
            missing.append({'namespace': 'related', 'role': 'build_metadata', 'path': rel, 'reason': 'missing_or_symlink'})
    for path, identity in frozen:
        current, _ = _stream_identity(path)
        if current != identity:
            raise ValueError(f'historical source changed during collection: {path}')
    collected_roles = {role: sum(run['roles'][role]['status'] == 'present' for run in runs) for role in ('run', 'detector', 'phase', 'server', 'environment')}
    unique_collected_roles = {role: len({(run['namespace'], run['roles'][role]['path']) for run in runs if run['roles'][role]['status'] == 'present'}) for role in collected_roles}
    report = {'inventory': inventory, 'runs': runs, 'missing': missing, 'exclusions': exclusions, 'expected_csv_run_references': 12, 'observed_csv_run_references': len(runs), 'collected_role_reference_counts': collected_roles, 'collected_unique_file_counts_by_role': unique_collected_roles}
    _json(output_dir / 'history_inventory.json', report)
    _exclusive(output_dir / 'README_PRIVATE.txt', PRIVATE_NOTICE.encode())
    return report


def archive_session(session_dir: Path, archive_path: Path) -> dict:
    """Archive eligible private evidence, including failed/cancelled runs.

    Caller must supply a unique target for every attempt. Failed/incomplete ZIPs
    are deliberately retained for diagnosis and are never overwritten or deleted.
    Only a returned PASS confirms complete verification.
    """
    root, target = Path(session_dir).absolute(), Path(archive_path).absolute()
    if root.is_symlink() or root.resolve() != root:
        raise ValueError('session root traverses symlink')
    if target.exists() or Path(str(target) + '.sha256').exists():
        raise FileExistsError(target)
    snapshots, entries, excluded = {}, [], []
    initial_paths = set(_walk(root)) - {target, Path(str(target) + '.sha256')}
    for path in sorted(initial_paths):
        if path == target or path == Path(str(target) + '.sha256'):
            continue
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in DISALLOWED_SUFFIXES:
            excluded.append(rel)
            continue
        if _binary_signature(path):
            excluded.append(rel)
            continue
        identity, prefix = _stream_identity(path)
        if rel in {'ARCHIVE_MANIFEST.json', 'ARCHIVE_PRIVATE_NOTICE.txt', 'ARCHIVE_VALIDATION_METHOD.md'}:
            raise ValueError('reserved archive member name')
        snapshots[rel] = (path, identity)
        entries.append({'path': rel, **identity})
    manifest = {'format': 'private-evidence-v1', 'files': entries, 'excluded': excluded}
    target.parent.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in target.parents):
        raise ValueError('symlink archive destination')
    with target.open('xb') as handle:
        with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as z:
            for rel, (path, identity) in snapshots.items():
                with z.open(rel, 'w', force_zip64=True) as member:
                    current, _ = _stream_identity(path, member)
                if current != identity:
                    raise ValueError(f'source changed before/during archive: {path}')
            z.writestr('ARCHIVE_MANIFEST.json', json.dumps(manifest, sort_keys=True).encode())
            z.writestr('ARCHIVE_PRIVATE_NOTICE.txt', PRIVATE_NOTICE)
            z.writestr('ARCHIVE_VALIDATION_METHOD.md', VALIDATION_METHOD)
    # Recheck after ZIP closure; no PASS is possible with a changed source.
    for path, identity in snapshots.values():
        current, _ = _stream_identity(path)
        if current != identity:
            raise ValueError(f'source changed during archive: {path}')
    if set(_walk(root)) - {target, Path(str(target) + '.sha256')} != initial_paths:
        raise ValueError('session file census changed during archive')
    digest = _stream_identity(target)[0]['sha256']
    _exclusive(Path(str(target) + '.sha256'), (digest + '  ' + target.name + '\n').encode())
    return validate_archive(target)


def validate_archive(path: Path, expected_sha256: str | None = None) -> dict:
    """Independently check safe names, CRC, complete member census and all hashes."""
    path = Path(path)
    try:
        archive_identity, _ = _stream_identity(path)
        digest = archive_identity['sha256']
        external_checked = False
        if expected_sha256 is not None:
            if digest != expected_sha256.lower():
                raise ValueError('supplied archive SHA256 mismatch')
            external_checked = True
        sidecar = Path(str(path) + '.sha256')
        if sidecar.exists() or sidecar.is_symlink():
            expected = _snapshot(sidecar)[0].decode().split()[0]
            if digest != expected:
                raise ValueError('external archive SHA256 mismatch')
            external_checked = True
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or any(not _safe_name(n) for n in names):
                raise ValueError('unsafe or duplicate ZIP paths')
            if any(stat.S_ISLNK(i.external_attr >> 16) for i in z.infolist()):
                raise ValueError('ZIP symlink')
            if z.testzip() is not None:
                raise ValueError('ZIP CRC mismatch')
            if z.read('ARCHIVE_PRIVATE_NOTICE.txt') != PRIVATE_NOTICE.encode() or z.read('ARCHIVE_VALIDATION_METHOD.md') != VALIDATION_METHOD.encode():
                raise ValueError('archive policy/validation note mismatch')
            manifest = json.loads(z.read('ARCHIVE_MANIFEST.json'))
            refs = [entry['path'] for entry in manifest['files']]
            if len(refs) != len(set(refs)) or set(names) != set(refs) | {'ARCHIVE_MANIFEST.json', 'ARCHIVE_PRIVATE_NOTICE.txt', 'ARCHIVE_VALIDATION_METHOD.md'}:
                raise ValueError('manifest member census mismatch')
            for entry in manifest['files']:
                if not isinstance(entry['mtime_ns'], int) or not _safe_name(entry['path']):
                    raise ValueError('invalid manifest metadata')
                member_hash, member_size = hashlib.sha256(), 0
                with z.open(entry['path']) as member:
                    while chunk := member.read(BLOCK_SIZE):
                        member_hash.update(chunk)
                        member_size += len(chunk)
                if member_size != entry['bytes'] or member_hash.hexdigest() != entry['sha256']:
                    raise ValueError('member SHA256/size mismatch')
        if _stream_identity(path)[0] != archive_identity:
            raise ValueError('archive changed during validation')
        return {'status': 'PASS', 'archive': str(path), 'sha256': digest, 'external_archive_hash_checked': external_checked, 'files': len(refs), 'excluded': manifest['excluded']}
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, zipfile.BadZipFile, UnicodeError, RuntimeError, zlib.error, EOFError) as exc:
        return {'status': 'FAIL', 'archive': str(path), 'error': str(exc)}


PRIVATE_ARCHIVE_KINDS = ('preflight', 'run_complete', 'run_stopped', 'run_failed', 'recovery')


def private_archive_path(session, kind, unique_id):
    """One private namespace for every producer, with an explicit purpose tag."""
    session = Path(session)
    if kind not in PRIVATE_ARCHIVE_KINDS or not re.fullmatch(r'[A-Za-z0-9_-]+', str(unique_id)):
        raise ValueError('Invalid private archive purpose or unique identifier')
    return session.parent / f'{session.name}_private_{kind}_{unique_id}.zip'


def verify_delivery(path, session):
    """Read-only delivery check against the FINAL local session snapshot.

    Caller holds the session lock. Ignore ONLY archive_elapsed ledger entries:
    the coordinator appends its own archival cost after closing its ZIP. All
    scientific, failure and completion events and eligible file contents must
    still match. This does not rerun analysis or modify any source evidence.
    """
    report = validate_archive(path)
    if report['status'] != 'PASS':
        return report
    path, root = Path(path).absolute(), Path(session).absolute()
    try:
        if root.is_symlink() or root.resolve() != root or not root.is_dir():
            raise ValueError('Unsafe or missing session directory')
        kinds = [kind for kind in PRIVATE_ARCHIVE_KINDS
                 if path.name.startswith(f'{root.name}_private_{kind}_')]
        if path.parent != root.parent or len(kinds) != 1:
            raise ValueError('Archive is not a recognized private output for this session')
        kind = kinds[0]
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read('ARCHIVE_MANIFEST.json'))
            # Lock creation is coordination bookkeeping, not session evidence.
            if any(entry['path'] == '.lock' and entry['bytes'] != 0 for entry in manifest['files']):
                raise ValueError('Nonempty archived coordination lock is not ignorable evidence')
            entries = {entry['path']: entry for entry in manifest['files'] if entry['path'] != '.lock'}
            current = {}
            for file in _walk(root):
                if file.relative_to(root).as_posix() == '.lock':
                    if file.stat().st_size != 0:
                        raise ValueError('Nonempty current coordination lock is not ignorable evidence')
                    continue
                if file.suffix.lower() in DISALLOWED_SUFFIXES or _binary_signature(file):
                    continue
                current[file.relative_to(root).as_posix()] = _stream_identity(file)[0]
            if set(current) != set(entries):
                raise ValueError('Archive is not the final session file census; do not send an earlier snapshot')
            for name, identity in current.items():
                if name == 'audit_ledger.jsonl':
                    continue
                if any(identity[key] != entries[name][key] for key in ('sha256', 'bytes')):
                    raise ValueError('Archive is not the final session content: ' + name)
            def events(data):
                return [event for line in data.splitlines() if line.strip()
                        and (event := json.loads(line)).get('kind') != 'archive_elapsed']
            ledger_path = root / 'audit_ledger.jsonl'
            live = events(ledger_path.read_bytes()) if ledger_path.exists() else []
            saved = events(z.read('audit_ledger.jsonl')) if 'audit_ledger.jsonl' in entries else []
            if live != saved:
                raise ValueError('Archive ledger is stale; final completions/stops differ')
            completed, measured = [], 0
            for event in saved:
                if event.get('kind') in ('request_completed', 'attempt_failed'):
                    attempt = event['attempt_id']
                    if Path(attempt).name != attempt or attempt in ('.', '..'):
                        raise ValueError('Invalid attempt reference in final ledger')
                    for name, checksum in event['raw_sha256'].items():
                        if checksum is None:
                            continue  # Empty directory entries are not ZIP files.
                        if not _safe_name(name):
                            raise ValueError('Unsafe raw evidence reference')
                        member = 'attempts/' + attempt + '/' + name
                        if entries.get(member, {}).get('sha256') != checksum:
                            raise ValueError('Missing or mismatched raw attempt evidence')
                if event.get('kind') != 'request_completed':
                    continue
                record = event['record']
                if not _safe_name(record) or entries.get(record, {}).get('sha256') != event['record_sha256']:
                    raise ValueError('Missing or mismatched completed measured-run record')
                value = json.loads(z.read(record))
                if value.get('run_id') != event['run_id'] or value.get('status') != 'ok':
                    raise ValueError('Completed run record disagrees with final ledger')
                completed.append(event['run_id'])
                measured += value.get('cohort') != 'technical_smoke'
            if len(completed) != len(set(completed)):
                raise ValueError('Duplicate completed-run references')
            ends = [i for i, e in enumerate(saved) if e.get('kind') == 'session_end']
            if ends:
                last = ends[-1]
                state = saved[last]['status']
                if any(e.get('kind') in ('terminal_stop', 'session_stopped', 'analysis_failed') for e in saved[last + 1:]):
                    state = 'STOPPED'
            else:
                state = 'STOPPED' if saved else 'PREFLIGHT_ONLY'
            if kind == 'preflight' and (saved or completed):
                raise ValueError('Preflight-only archive is not experimental result delivery')
            if kind == 'run_complete' and state != 'COMPLETE':
                raise ValueError('Completed-run archive does not describe a completed session')
        return {**report, 'verification_scope': 'final_session_snapshot', 'archive_kind': kind,
                'session_state': state, 'completed_requests': len(completed),
                'measured_requests': measured, 'delivery_ready': True}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError,
            zipfile.BadZipFile, UnicodeError, zlib.error, EOFError) as exc:
        return {**report, 'status': 'FAIL', 'delivery_ready': False, 'error': str(exc)}
