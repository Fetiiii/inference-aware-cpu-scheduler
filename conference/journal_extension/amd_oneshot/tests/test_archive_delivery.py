"""Synthetic final-delivery regressions; no inference, builds or network calls."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from conference.journal_extension.amd_oneshot import archive, cli, coordinator, distribution
from conference.journal_extension.amd_oneshot.common import ledger, load_json
from conference.journal_extension.amd_oneshot.tests import test_coordinator as fixtures


class ArchiveDeliveryTests(unittest.TestCase):
    def test_archive_path_is_explicitly_private_and_outside_session(self):
        session = Path('/synthetic/results/journal_extension_amd_01')
        for kind in ('preflight', 'run_complete', 'run_stopped', 'recovery', 'run_failed'):
            target = archive.private_archive_path(session, kind, 'synthetic-id')
            self.assertEqual(target, session.parent / (session.name + '_private_' + kind + '_synthetic-id.zip'))
            self.assertFalse(target.is_relative_to(session))

    def fixture(self):
        f = fixtures.CoordinatorTests(methodName='test_full_72_and_completed_never_rerun')
        f.setUp()
        self.addCleanup(f.doCleanups)
        target = f.session.parent / 'results' / 'journal_extension_amd_synthetic'
        target.parent.mkdir()
        f.session.rename(target)
        f.session = target
        f.deps['archive_builder'] = archive.archive_session
        return f

    def final(self, f, count, state='COMPLETE', kind='run_complete'):
        result = f.run_session()
        self.assertEqual(result['status'], state)
        target = Path(result['final_archive']['archive'])
        self.assertIn('_private_' + kind + '_', target.name)
        self.assertEqual(archive.validate_archive(target)['status'], 'PASS')
        report = archive.verify_delivery(target, f.session)
        self.assertEqual(report['status'], 'PASS', report)
        self.assertEqual(report['archive_kind'], kind)
        self.assertEqual(report['session_state'], state)
        self.assertEqual(report['completed_requests'], count)
        self.assertEqual(report['measured_requests'], max(0, count - 4))
        return target

    def test_full72_final_contains_records_raw_and_rejects_stale_preflight(self):
        f = self.fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            early = Path(cli.final_archive(f.session)['archive'])
        self.assertIn('_private_preflight_', early.name)
        self.assertEqual(archive.verify_delivery(early, f.session)['session_state'], 'PREFLIGHT_ONLY')
        final = self.final(f, 72)
        self.assertNotEqual(early, final)
        self.assertEqual(archive.verify_delivery(early, f.session)['status'], 'FAIL')
        with zipfile.ZipFile(final) as z:
            for event in ledger(f.session):
                if event['kind'] == 'request_completed':
                    self.assertEqual(z.read(event['record']), (f.session / event['record']).read_bytes())
                    raw = 'attempts/' + event['attempt_id'] + '/synthetic_raw.json'
                    self.assertEqual(z.read(raw), (f.session / raw).read_bytes())

    def test_static_signal48_final(self):
        f = self.fixture()
        def slow_smoke(*args):
            record = f.backend(*args)
            if record['cohort'] == 'technical_smoke':
                record['startup_elapsed_s'] = 100
            return record
        f.deps['backend'] = slow_smoke
        self.final(f, 48)
        self.assertEqual(load_json(f.session / 'tier_decision.json')['selected_tier'], 'STATIC_SIGNAL')

    def test_cancelled_request_keeps_partial_raw_and_prior_completed_record(self):
        f = self.fixture()
        def interrupted(*args):
            record = f.backend(*args)
            if len(f.calls) == 2:
                raise KeyboardInterrupt('SYNTHETIC CANCEL')
            return record
        f.deps['backend'] = interrupted
        target = self.final(f, 1, state='STOPPED', kind='run_stopped')
        with zipfile.ZipFile(target) as z:
            self.assertEqual(sum(name.endswith('/synthetic_raw.json') for name in z.namelist()), 2)

    def test_nonempty_lock_is_not_silently_ignored_as_final_evidence(self):
        f = self.fixture()
        target = self.final(f, 72)
        (f.session / '.lock').write_text('SYNTHETIC EXTRA DATA')
        self.assertEqual(archive.verify_delivery(target, f.session)['status'], 'FAIL')

    def test_not_eligible48_preserves_frozen_model_during_packaging_and_verify(self):
        f = self.fixture()
        f.deps['gate_policy'] = lambda exploration, *args: {
            'status': 'NOT_ELIGIBLE', 'selection_hash': fixtures.digest(exploration),
            'reasons': ['SYNTHETIC_ONLY']}
        target = self.final(f, 48)
        frozen = f.session / 'frozen_detector.json'
        original = frozen.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            recovery = cli.final_archive(f.session, kind='recovery')
        self.assertEqual(archive.verify_delivery(Path(recovery['archive']), f.session)['status'], 'PASS')
        self.assertEqual(archive.verify_delivery(target, f.session)['status'], 'PASS')
        self.assertEqual(frozen.read_bytes(), original)

    def test_guard_stop_retains_completed_record_and_raw(self):
        f = self.fixture()
        def mutate_guarded_input(*args):
            record = f.backend(*args)
            (f.session / 'source' / 'synthetic.json').write_text('{"synthetic":"changed during request"}')
            return record
        f.deps['backend'] = mutate_guarded_input
        target = self.final(f, 1, state='STOPPED', kind='run_stopped')
        event = next(e for e in ledger(f.session) if e['kind'] == 'request_completed')
        with zipfile.ZipFile(target) as z:
            self.assertIn(event['record'], z.namelist())
            self.assertIn('attempts/' + event['attempt_id'] + '/synthetic_raw.json', z.namelist())

    def test_delivery_rejects_new_file_modified_record_and_raw(self):
        for mutation in ('new', 'record', 'raw'):
            with self.subTest(mutation=mutation):
                f = self.fixture()
                target = self.final(f, 72)
                event = next(e for e in ledger(f.session) if e['kind'] == 'request_completed')
                path = {'new': f.session / 'unarchived.log',
                        'record': f.session / event['record'],
                        'raw': f.session / 'attempts' / event['attempt_id'] / 'synthetic_raw.json'}[mutation]
                path.write_text('SYNTHETIC changed')
                self.assertEqual(archive.validate_archive(target)['status'], 'PASS')
                self.assertEqual(archive.verify_delivery(target, f.session)['status'], 'FAIL')

    def test_documented_cli_verify_accepts_produced_filename(self):
        f = self.fixture()
        target = self.final(f, 72)
        with patch.object(distribution, 'CODE_ROOT', f.session.parent.parent), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['verify', str(target), '--session', str(f.session)]), 0)
            self.assertEqual(cli.main(['verify', str(target)]), 0)
        self.assertIn('PASS', out.getvalue())

    def test_cli_run_prints_exact_final_archive_and_delivery_status(self):
        f = self.fixture()
        result = f.run_session()
        approval_inputs = tuple(load_json(f.session / name) for name in
                                ('plan.json', 'config.json', 'execution_authorized.json', 'preflight.json'))
        with patch.object(distribution, 'CODE_ROOT', f.session.parent.parent), \
             patch.object(cli, 'authorize_run', return_value=approval_inputs), \
             patch.object(cli, 'prepare_runtime', return_value=None), \
             patch.object(coordinator, 'execute_session', return_value=result), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['run', '--session', str(f.session), '--confirm']), 0)
        self.assertIn('FINAL RUN ARCHIVE:', out.getvalue())
        self.assertIn(result['final_archive']['archive'], out.getvalue())
        self.assertIn('PASS', out.getvalue())

    def test_matching_current_snapshot_still_requires_completed_record_and_raw_hashes(self):
        for mutation in ('record', 'raw'):
            with self.subTest(mutation=mutation):
                f = self.fixture()
                self.final(f, 72)
                event = next(e for e in ledger(f.session) if e['kind'] == 'request_completed')
                path = (f.session / event['record'] if mutation == 'record' else
                        f.session / 'attempts' / event['attempt_id'] / 'synthetic_raw.json')
                path.write_text('{"synthetic":"modified after completion"}')
                target = archive.private_archive_path(f.session, 'recovery', mutation)
                archive.archive_session(f.session, target)
                self.assertEqual(archive.validate_archive(target)['status'], 'PASS')
                self.assertEqual(archive.verify_delivery(target, f.session)['status'], 'FAIL')

    def test_packaging_does_not_change_scientific_sources(self):
        root = Path(__file__).resolve().parents[4]
        paths = [root / 'conference/journal_extension/amd_oneshot' / name
                 for name in ('calibration.py', 'protocol.py', 'runtime.py', 'runtime_fallback.py', 'analysis.py')]
        paths += list((root / 'conference/journal_extension').rglob('RUN_PLAN*'))
        paths += list((root / 'conference/journal_extension').rglob('HISTORICAL_IDENTITY*'))
        before = {path: path.read_bytes() for path in paths if path.is_file()}
        self.assertGreaterEqual(len(before), 4)
        self.final(self.fixture(), 72)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_private_and_legacy_archives_cannot_be_added_normally(self):
        root = Path(__file__).resolve().parents[4]
        with tempfile.TemporaryDirectory(prefix='synthetic-ignore-') as name:
            repo = Path(name)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            shutil.copyfile(root / '.gitignore', repo / '.gitignore')
            names = ['journal_extension_amd_01.zip',
                     'journal_extension_amd_01_123456.zip',
                     'journal_extension_amd_01_segment_abc.zip',
                     'journal_extension_amd_01_private_123456.zip']
            names += ['journal_extension_amd_01_private_' + kind + '_abc.zip'
                      for kind in ('preflight', 'run_complete', 'run_stopped', 'recovery', 'run_failed')]
            for name in names:
                for suffix in ('', '.sha256'):
                    relative = Path('results') / (name + suffix)
                    path = repo / relative
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(b'SYNTHETIC private evidence')
                    ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', str(relative)], capture_output=True)
                    self.assertEqual(ignored.returncode, 0, str(relative))
                    add = subprocess.run(['git', '-C', str(repo), 'add', '--', str(relative)], capture_output=True)
                    self.assertNotEqual(add.returncode, 0, str(relative))
            staged = subprocess.run(['git', '-C', str(repo), 'diff', '--cached', '--name-only'], capture_output=True, check=True)
            self.assertEqual(staged.stdout, b'')


if __name__ == '__main__':
    unittest.main()
