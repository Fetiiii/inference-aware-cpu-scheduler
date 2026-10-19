"""SYNTHETIC fixtures only: no project experiment results are touched."""
import csv
import hashlib
import importlib.util
import io
import json
import os
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('amd_archive', Path(__file__).parents[1] / 'archive.py')
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class SyntheticArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='SYNTHETIC_')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data=b'SYNTHETIC evidence\n'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_history_preserves_and_reports_each_reference(self):
        root = self.root / 'repo/results/conference_c03'
        rows = []
        for n in range(1, 13):
            rows.append(dict(round=n, sequence_index=1, arm='BIG_ONLY', detector_file=f'raw/detector/{n}.json', phase_log=f'raw/phase_logs/{n}.json', server_log=f'raw/server_logs/{n}.log', environment_file='raw/environment/env.json'))
        out = io.StringIO()
        writer = csv.DictWriter(out, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        self.write('repo/results/conference_c03/c03_runs.csv', out.getvalue().encode())
        summary = self.write('repo/results/conference_c03/summary.json', b'{"SYNTHETIC":"analyzer output"}')
        evidence = self.write('repo/results/conference_c03/raw/detector/1.json')
        os.utime(evidence, ns=(123456789000, 123456789000))
        self.write('repo/results/conference_c03/personal.txt')
        self.write('repo/results/conference_c03/raw/model.gguf', b'GGUFdata')
        self.write('repo/results/conference_c03/raw/tool.log', b'\x7fELFbinary')
        self.write('repo/unrelated/secret.txt')
        outside = self.write('outside.txt')
        (root / 'raw/link.log').symlink_to(outside)
        report = archive.collect_history(self.root / 'repo', self.root / 'out')
        self.assertEqual(len(report['runs']), 12)
        self.assertEqual(report['observed_csv_run_references'], 12)
        self.assertEqual(report['collected_role_reference_counts']['run'], 0)
        self.assertEqual(report['collected_role_reference_counts']['detector'], 1)
        self.assertEqual(report['collected_unique_file_counts_by_role']['detector'], 1)
        self.assertEqual(report['runs'][0]['roles']['detector']['status'], 'present')
        self.assertEqual(report['runs'][0]['roles']['run']['status'], 'missing_or_unsafe')
        copied = self.root / 'out/history/c03_00/raw/detector/1.json'
        self.assertEqual(copied.read_bytes(), evidence.read_bytes())
        self.assertEqual(copied.stat().st_mtime_ns, evidence.stat().st_mtime_ns)
        self.assertEqual(len(report['inventory']), 3)
        self.assertEqual((self.root / 'out/history/c03_00/summary.json').read_bytes(), summary.read_bytes())
        self.assertTrue(any(m['role'] == 'original_plan' for m in report['missing']))
        related_missing = {m['path'] for m in report['missing'] if m['namespace'] == 'related'}
        self.assertEqual(related_missing, set(archive.RELATED_METADATA))
        persisted = json.loads((self.root / 'out/history_inventory.json').read_text())
        self.assertEqual(persisted['missing'], report['missing'])
        with self.assertRaises(FileExistsError):
            archive.collect_history(self.root / 'repo', self.root / 'out')

    def test_refuses_broad_roots_and_symlink_escape(self):
        for candidate in [Path('/'), Path.home(), self.root]:
            with self.assertRaises(ValueError):
                archive.collect_history(self.root / 'repo', self.root / 'out', [candidate])
        real = self.root / 'real'
        real.mkdir()
        link = self.root / 'conference_c03'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            archive.collect_history(self.root / 'repo', self.root / 'out', [link])

    def test_private_failed_session_archive_and_corruption(self):
        self.write('session/failure.json', b'{"status":"cancelled", "SYNTHETIC":true}')
        self.write('session/source.py')
        license_file = self.write('session/source_snapshot/support/LICENSE', b'SYNTHETIC approved license text\n')
        documentation = self.write('session/source_snapshot/docs.json', b'{"SYNTHETIC":"source documentation"}')
        self.write('session/weights.gguf', b'GGUFpayload')
        self.write('session/server', b'\x7fELFpayload')
        (self.root / 'session/outside.log').symlink_to(self.write('secret'))
        target = self.root / 'evidence.zip'
        result = archive.archive_session(self.root / 'session', target)
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['external_archive_hash_checked'])
        self.assertEqual(result['files'], 4)
        with zipfile.ZipFile(target) as z:
            self.assertIsNone(z.testzip())
            self.assertNotIn('weights.gguf', z.namelist())
            self.assertIn('ARCHIVE_PRIVATE_NOTICE.txt', z.namelist())
            self.assertIn('ARCHIVE_VALIDATION_METHOD.md', z.namelist())
            self.assertEqual(z.read('source_snapshot/support/LICENSE'), license_file.read_bytes())
            self.assertEqual(z.read('source_snapshot/docs.json'), documentation.read_bytes())
        with self.assertRaises(FileExistsError):
            archive.archive_session(self.root / 'session', target)
        target.write_bytes(target.read_bytes()[:-8])
        self.assertEqual(archive.validate_archive(target)['status'], 'FAIL')

    def test_manifest_digest_and_traversal_independent_check(self):
        for name, wrong_digest in [('safe.txt', True), ('../escape.txt', False)]:
            target = self.root / ('bad' + str(wrong_digest) + '.zip')
            payload = b'SYNTHETIC'
            with zipfile.ZipFile(target, 'w') as z:
                z.writestr(name, payload)
                z.writestr('ARCHIVE_PRIVATE_NOTICE.txt', archive.PRIVATE_NOTICE)
                z.writestr('ARCHIVE_VALIDATION_METHOD.md', archive.VALIDATION_METHOD)
                z.writestr('ARCHIVE_MANIFEST.json', json.dumps({'files': [{'path': name, 'sha256': 'bad' if wrong_digest else hashlib.sha256(payload).hexdigest(), 'bytes': len(payload), 'mtime_ns': 0}], 'excluded': []}))
            Path(str(target) + '.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(archive.validate_archive(target)['status'], 'FAIL')

    def test_mutation_fails_without_pass_sidecar(self):
        file = self.write('session/run.log')
        original = archive._stream_identity
        count = 0

        def mutate(path, sink=None):
            nonlocal count
            if path == file:
                count += 1
                if count == 2:
                    file.write_bytes(b'SYNTHETIC changed')
            return original(path, sink)

        target = self.root / 'mutated.zip'
        with patch.object(archive, '_stream_identity', side_effect=mutate):
            with self.assertRaises(ValueError):
                archive.archive_session(self.root / 'session', target)
        self.assertFalse(Path(str(target) + '.sha256').exists())
        self.assertTrue(target.exists(), 'failed ZIP is retained for diagnosis')
        with self.assertRaises(FileExistsError):
            archive.archive_session(self.root / 'session', target)

    def test_crc_corruption_even_with_matching_external_hash(self):
        self.write('session/run.log', b'SYNTHETIC repeated evidence' * 10)
        target = self.root / 'crc.zip'
        archive.archive_session(self.root / 'session', target)
        data = bytearray(target.read_bytes())
        with zipfile.ZipFile(target) as z:
            info = z.getinfo('run.log')
            name_len, extra_len = struct.unpack_from('<HH', data, info.header_offset + 26)
            offset = info.header_offset + 30 + name_len + extra_len
        data[offset] ^= 0xff
        target.write_bytes(data)
        Path(str(target) + '.sha256').write_text(hashlib.sha256(data).hexdigest())
        self.assertEqual(archive.validate_archive(target)['status'], 'FAIL')

    def test_history_mutation_during_copy_fails(self):
        source = self.write('repo/results/conference_c03/raw/plan.json')
        original = archive._exclusive

        def mutate(path, data, mtime=None):
            original(path, data, mtime)
            if path.name == 'plan.json':
                source.write_bytes(b'SYNTHETIC mutation')

        with patch.object(archive, '_exclusive', side_effect=mutate):
            with self.assertRaises(ValueError):
                archive.collect_history(self.root / 'repo', self.root / 'out')
        self.assertFalse((self.root / 'out/history_inventory.json').exists())

    def test_archive_inside_session_excludes_itself(self):
        self.write('session/run.log')
        target = self.root / 'session/evidence.zip'
        report = archive.archive_session(self.root / 'session', target)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['files'], 1)
        with zipfile.ZipFile(target) as z:
            self.assertNotIn('evidence.zip', z.namelist())

    def test_standalone_zip_and_supplied_external_hash(self):
        self.write('session/run.log')
        original = self.root / 'original.zip'
        report = archive.archive_session(self.root / 'session', original)
        standalone = self.root / 'standalone.zip'
        standalone.write_bytes(original.read_bytes())
        checked = archive.validate_archive(standalone)
        self.assertEqual(checked['status'], 'PASS')
        self.assertFalse(checked['external_archive_hash_checked'])
        checked = archive.validate_archive(standalone, expected_sha256=report['sha256'])
        self.assertEqual(checked['status'], 'PASS')
        self.assertTrue(checked['external_archive_hash_checked'])
        self.assertEqual(archive.validate_archive(standalone, expected_sha256='0' * 64)['status'], 'FAIL')

    def test_obvious_models_are_never_read_and_session_streams(self):
        self.write('repo/results/conference_c03/raw/model.gguf', b'GGUF SYNTHETIC')
        self.write('session/weights.gguf', b'GGUF SYNTHETIC')
        self.write('session/payload.log', b'SYNTHETIC' * (archive.BLOCK_SIZE // 3))
        snapshot = archive._snapshot

        def guarded_snapshot(path):
            self.assertNotEqual(path.suffix, '.gguf')
            self.assertNotEqual(path.name, 'payload.log')
            self.assertNotEqual(path.suffix, '.zip')
            return snapshot(path)

        with patch.object(archive, '_snapshot', side_effect=guarded_snapshot):
            archive.collect_history(self.root / 'repo', self.root / 'out')
            report = archive.archive_session(self.root / 'session', self.root / 'stream.zip')
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['files'], 1)


if __name__ == '__main__':
    unittest.main()
