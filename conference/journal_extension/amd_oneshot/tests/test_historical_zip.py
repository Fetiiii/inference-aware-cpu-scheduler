import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from conference.journal_extension.amd_oneshot import historical_zip as hz


class HistoricalZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def make(self, entries, compression=zipfile.ZIP_STORED):
        source = self.base / 'input.zip'
        with zipfile.ZipFile(source, 'w', compression=compression) as z:
            for name, data in entries:
                z.writestr(name, data)
        return source, hashlib.sha256(source.read_bytes()).hexdigest()

    def ingest(self, entries):
        source, digest = self.make(entries)
        return hz.ingest_archive(source, self.base / 'out', digest)

    def test_bytes_hash_timestamps_and_source_unchanged(self):
        info = zipfile.ZipInfo('results/conference_c03/raw/a.txt', (2024, 2, 3, 4, 5, 6))
        source, digest = self.make([(info, b'evidence\r\n')])
        before = source.stat()
        report = hz.ingest_archive(source, self.base / 'out', digest)
        self.assertEqual((Path(report['root']) / 'raw/a.txt').read_bytes(), b'evidence\r\n')
        self.assertEqual(report['members'][0]['date_time'], [2024, 2, 3, 4, 5, 6])
        self.assertEqual(report['members'][0]['sha256'], hashlib.sha256(b'evidence\r\n').hexdigest())
        self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(report['source_sha256_after'], digest)
        self.assertEqual(stat.S_IMODE((self.base / 'out').stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            hz.ingest_archive(source, self.base / 'out', digest)

    def test_sha_rejected_before_destination(self):
        source, _ = self.make([('results/conference_c03/a.txt', b'a')])
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            hz.ingest_archive(source, self.base / 'out')
        self.assertFalse((self.base / 'out').exists())

    def test_unsafe_names(self):
        for name in ('../a', '/a', 'results/conference_c03/../a', 'results\\conference_c03\\a',
                     'results/conference_c03/./a', 'results/conference_c03//a', 'C:/a', 'other/a'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ingest([(name, b'a')])

    def test_collisions(self):
        for names in [('a', 'a'), ('a', 'A'), ('a', 'a/b')]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.ingest([('results/conference_c03/' + n, b'a') for n in names])

    def test_symlink_and_special(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK):
            info = zipfile.ZipInfo('results/conference_c03/a')
            info.create_system = 3
            info.external_attr = (kind | 0o777) << 16
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.ingest([(info, b'target')])

    def test_models_and_binary_content(self):
        for name, data in [('a.gguf', b'weights'), ('a.txt', b'\x7fELF'), ('a.txt', b'GGUF'), ('a.txt', b'a\x00b')]:
            # Failed private destinations are deliberately retained.
            dest = self.base / ('out' + str(len(list(self.base.iterdir()))))
            source, digest = self.make([('results/conference_c03/' + name, data)])
            with self.subTest(name=name, data=data), self.assertRaises(ValueError):
                hz.ingest_archive(source, dest, digest)

    def test_bomb_bounds(self):
        source, digest = self.make([('results/conference_c03/a.txt', b'a' * 10000)], zipfile.ZIP_DEFLATED)
        with patch.object(hz, 'MAX_RATIO', 2), self.assertRaises(ValueError):
            hz.ingest_archive(source, self.base / 'out', digest)
        with patch.object(hz, 'MAX_TOTAL_BYTES', 100), self.assertRaises(ValueError):
            hz.ingest_archive(source, self.base / 'out', digest)

    def test_crc_rejected_even_with_matching_archive_sha(self):
        source, _ = self.make([('results/conference_c03/a.txt', b'unique-evidence')])
        data = source.read_bytes().replace(b'unique-evidence', b'broken-evidence', 1)
        source.write_bytes(data)
        with self.assertRaises(zipfile.BadZipFile):
            hz.ingest_archive(source, self.base / 'out', hashlib.sha256(data).hexdigest())

    def test_encrypted_flag_rejected(self):
        source, _ = self.make([('results/conference_c03/a.txt', b'a')])
        data = bytearray(source.read_bytes())
        central = data.index(b'PK\x01\x02')
        data[6] |= 1
        data[central + 8] |= 1
        source.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            hz.ingest_archive(source, self.base / 'out', hashlib.sha256(data).hexdigest())

    def test_recovered_mutation_rejected(self):
        report = self.ingest([('results/conference_c03/a.txt', b'original')])
        (Path(report['root']) / 'a.txt').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            hz.validate_recovered(report, {})

    def test_missing_census_fails_gate(self):
        report = self.ingest([('results/conference_c03/summary.txt', b'a')])
        plan = {'runtime': dict(ctx=1, batch=1, ubatch=1, n_predict=1, seed=1),
                'cooldown_s': 1, 'initial_cooldown_s': 1}
        result = hz.validate_recovered(report, plan)
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertTrue(any('twelve' in e for e in result['errors']))

    def test_census_and_binary_gate(self):
        roles = ('run', 'detector', 'phase', 'server', 'environment')
        for defect in ('none', 'duplicate_round', 'missing_role', 'binary', 'environment_count'):
            report = {'root': str(self.base / 'unused'), 'destination': str(self.base / defect),
                      'members': [], 'status': 'PASS', 'source_sha256_before': 'test'}
            Path(report['destination']).mkdir()
            runs = [{'round': str(r), 'arm': arm, 'roles': {role: {'status': 'present'} for role in roles}}
                    for r in range(1, 7) for arm in ('BIG_ONLY', 'ALL_CORES')]
            counts = dict(run=12, detector=12, phase=12, server=12, environment=2)
            records = [{'llama_cpp': {'diagnostic_server_binary': {'sha256': hz.EXPECTED_BINARY_SHA256}}}
                       for _ in range(2)]
            if defect == 'duplicate_round':
                runs[-1]['round'] = '1'
            if defect == 'missing_role':
                runs[0]['roles']['phase']['status'] = 'missing'
            if defect == 'binary':
                records[0]['llama_cpp']['diagnostic_server_binary']['sha256'] = 'wrong'
            if defect == 'environment_count':
                counts['environment'] = 1
            with patch.object(hz.archive, 'collect_history', return_value={
                    'runs': runs, 'collected_unique_file_counts_by_role': counts}), patch.object(
                    hz.historical, 'check_anchor', return_value={'errors': [], 'records': records}):
                result = hz.validate_recovered(report, {})
            self.assertEqual(result['status'], 'PASS' if defect == 'none' else 'NOT_READY')


if __name__ == '__main__':
    unittest.main()
