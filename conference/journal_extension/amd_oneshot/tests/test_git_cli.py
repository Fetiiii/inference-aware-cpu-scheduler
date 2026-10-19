"""Git delivery CLI gates with synthetic metadata; no executable is started."""
import argparse
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import cli, git_delivery, protocol, runtime_fallback
from conference.journal_extension.amd_oneshot.common import digest, load_json, save_json


class GitCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-git-cli-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.session = self.repo / 'results/journal_extension_amd_01'
        self.plan = protocol.load_plan()
        self.files = {'synthetic.json': 'f' * 64}
        self.git = {'schema': 'git-native-source-v1', 'commit_sha': 'a' * 40,
                    'git_tree': 'b' * 40, 'required_files': self.files,
                    'source_sha256': digest(self.files),
                    'pinned_submodule_commit': self.plan['source_revision']}
        self.calls = []

    @contextlib.contextmanager
    def preflight_mocks(self, report=None, failure=None):
        report = report or {'status': 'PASS', 'errors': [], 'warnings': []}
        def verify(*args, **kwargs):
            self.calls.append('git')
            return copy.deepcopy(self.git)
        def snapshot(*args, **kwargs):
            self.calls.append('source')
            return {'identity_kind': 'git', 'git_commit': self.git['commit_sha'],
                    'package_sha256': self.git['source_sha256'], 'files': self.files, 'git_source': self.git}
        def identity(*args):
            self.calls.append('identity')
            save_json(self.session / 'historical_identity.json', {'synthetic': True, 'raw_records_staged': False})
            return {'diagnostic_build': {'synthetic': True}}
        def machine(config, destination):
            self.calls.append('machine')
            self.assertTrue((self.session / 'source_manifest.json').is_file())
            self.assertTrue((self.session / 'historical_identity.json').is_file())
            self.assertEqual(config['identity_kind'], 'git')
            if failure is not None:
                raise failure
            return copy.deepcopy(report)
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch.object(git_delivery, 'verify_checkout', side_effect=verify), \
             patch.object(git_delivery, 'snapshot_source', side_effect=snapshot), \
             patch.object(cli.historical_identity, 'stage_identity', side_effect=identity), \
             patch.object(cli.historical_zip, 'ingest_archive', side_effect=AssertionError('No private ZIP')) as ingest, \
             patch.object(cli.preflight, 'perform_preflight', side_effect=machine), \
             patch.object(runtime_fallback, 'build_fallback', side_effect=AssertionError('No compilation')), \
             patch('subprocess.Popen', side_effect=AssertionError('No process permitted')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield
            ingest.assert_not_called()

    def assert_archive(self):
        archives = list((self.repo / 'results').glob('*private*.zip'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(cli.archive.validate_archive(archives[0])['status'], 'PASS')

    def test_default_preflight_without_zip_stages_before_machine(self):
        with self.preflight_mocks():
            self.assertEqual(cli.main(['preflight']), 0)
        self.assertEqual(self.calls, ['git', 'source', 'identity', 'machine'])
        config = load_json(self.session / 'config.json')
        self.assertEqual(config['model'], str(self.repo / 'models' / self.plan['model_name']))
        self.assertEqual(config['server_bin'], str(self.repo / 'llama.cpp/build-diag/bin/llama-server'))
        self.assert_archive()

    def test_missing_model_remains_hard_gate_with_archive(self):
        with self.preflight_mocks({'status': 'NOT_READY', 'errors': ['model missing'], 'warnings': []}):
            self.assertEqual(cli.main(['preflight']), 2)
        self.assertEqual(load_json(self.session / 'preflight.json')['status'], 'NOT_READY')
        self.assert_archive()

    def test_missing_runtime_is_pending_only_with_other_gates_passed(self):
        for machine_errors, expected in (([], 'RUNTIME_FALLBACK_PENDING'), (['battery'], 'NOT_READY')):
            with self.subTest(errors=machine_errors), tempfile.TemporaryDirectory() as name:
                original_repo, original_session = self.repo, self.session
                self.repo = Path(name)
                self.session = self.repo / 'results/journal_extension_amd_01'
                report = {'status': 'NOT_READY', 'warnings': [],
                          'errors': ['build provenance: ORIGINAL_BINARY_MISSING: synthetic'] + machine_errors}
                try:
                    with self.preflight_mocks(report):
                        self.assertEqual(cli.main(['preflight']), 2)
                    self.assertEqual(load_json(self.session / 'preflight.json')['status'], expected)
                    self.assert_archive()
                finally:
                    self.repo, self.session = original_repo, original_session

    def stage_authorization_session(self):
        self.session.mkdir(parents=True)
        save_json(self.session / 'plan.json', self.plan)
        save_json(self.session / 'preflight.json', {'status': 'PASS'})
        save_json(self.session / 'git_provenance.json', self.git)
        save_json(self.session / 'config.json', {'identity_kind': 'git', 'git_commit': self.git['commit_sha'],
                  'repo_root': str(self.repo), 'package_sha256': self.git['source_sha256']})

    def approval(self, fallback=False):
        approval = git_delivery.approval_template(self.plan, self.git)
        approval.update(authorized=True, author_review_id='SYNTHETIC TEST ONLY', runtime_fallback_authorized=fallback)
        return approval

    def test_authorize_exact_git_commit_and_reject_other_commit(self):
        self.stage_authorization_session()
        approval_path = self.repo / 'synthetic-approval.json'
        save_json(approval_path, self.approval())
        args = argparse.Namespace(approval=approval_path)
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch.object(git_delivery, 'verify_checkout', return_value=self.git) as verify, \
             patch.object(cli.historical_identity, 'verify_session_identity', return_value={}):
            self.assertEqual(cli.authorize_run(args, self.session)[2], self.approval())
        verify.assert_called_once_with(str(self.repo), expected_commit='a' * 40)
        changed = copy.deepcopy(self.git)
        changed['commit_sha'] = 'c' * 40
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch.object(git_delivery, 'verify_checkout', return_value=changed), \
             patch.object(cli.historical_identity, 'verify_session_identity', return_value={}), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'provenance changed'):
            cli.authorize_run(args, self.session)

    def test_stale_zip_approval_and_missing_approval_rejected(self):
        self.stage_authorization_session()
        approval_path = self.repo / 'synthetic-legacy.json'
        stale = cli.review_template(self.plan, self.git['source_sha256'])
        stale.update(authorized=True, author_review_id='SYNTHETIC LEGACY TEST ONLY')
        save_json(approval_path, stale)
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch.object(git_delivery, 'verify_checkout', return_value=self.git), \
             patch.object(cli.historical_identity, 'verify_session_identity', return_value={}), \
             patch.object(cli, 'final_archive') as archive:
            for path in (None, approval_path):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    cli.authorize_run(argparse.Namespace(approval=path), self.session)
            self.assertEqual(archive.call_count, 2)
        self.assertFalse((self.session / 'execution_authorized.json').exists())

    def test_run_without_confirm_never_authorizes_or_executes(self):
        with patch.object(cli, 'authorize_run', side_effect=AssertionError('No authorization path')), \
             patch('subprocess.Popen', side_effect=AssertionError('No process')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['run', '--session', str(self.session)]), 2)

    def test_runtime_fallback_validates_git_bindings_without_compilation(self):
        config = {'plan': self.plan, 'identity_kind': 'git', 'repo_root': str(self.repo),
                  'git_commit': self.git['commit_sha'], 'package_sha256': self.git['source_sha256']}
        with patch.object(git_delivery, 'verify_checkout', return_value=self.git) as verify, \
             patch('subprocess.Popen', side_effect=AssertionError('No compilation')):
            runtime_fallback.validate_approval(config, self.approval(fallback=True))
            verify.assert_called_with(str(self.repo), expected_commit='a' * 40)
            for approval in (self.approval(), {**self.approval(True), 'git_commit': 'c' * 40},
                             cli.review_template(self.plan, self.git['source_sha256'])):
                with self.subTest(approval=approval), self.assertRaises(ValueError):
                    runtime_fallback.validate_approval(config, approval)

    def test_foreign_repo_rejected_before_snapshot(self):
        with self.preflight_mocks():
            self.assertEqual(cli.main(['preflight', '--repo', str(self.repo / 'foreign')]), 2)
        self.assertFalse(self.calls)
        self.assertFalse(self.session.exists())

    def test_foreign_execution_checkout_rejected_before_authorization(self):
        self.stage_authorization_session()
        with patch.object(git_delivery, 'verify_checkout', side_effect=AssertionError('Do not verify another checkout')), \
             patch.object(cli, 'final_archive') as archive:
            with self.assertRaisesRegex(ValueError, 'executing Git source'):
                cli.authorize_run(argparse.Namespace(approval=None), self.session)
        archive.assert_called_once()

    def test_unrelated_directory_never_archived_or_used_for_run(self):
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch.object(cli, 'final_archive', side_effect=AssertionError('No unrelated archive')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['package', '--session', str(self.repo)]), 2)
            self.assertEqual(cli.main(['run', '--session', str(self.repo), '--confirm']), 2)

    def test_locate_defaults_are_safe(self):
        output = io.StringIO()
        with patch.object(cli.distribution, 'CODE_ROOT', self.repo), \
             patch('subprocess.Popen', side_effect=AssertionError('No process')), \
             patch.object(git_delivery, 'verify_checkout', side_effect=AssertionError('Locate is metadata only')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['locate']), 0)
        self.assertIn(str(self.repo / 'models' / self.plan['model_name']), output.getvalue())
        self.assertIn(str(self.repo / 'llama.cpp/build-diag/bin/llama-server'), output.getvalue())

    def test_preflight_failure_and_cancellation_archive(self):
        for failure in (ValueError('synthetic failure'), KeyboardInterrupt('synthetic cancellation')):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as name:
                original_repo, original_session = self.repo, self.session
                self.repo = Path(name)
                self.session = self.repo / 'results/journal_extension_amd_01'
                try:
                    with self.preflight_mocks(failure=failure):
                        if isinstance(failure, KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                cli.main(['preflight'])
                        else:
                            self.assertEqual(cli.main(['preflight']), 2)
                    self.assert_archive()
                    self.assertEqual(len(list(self.session.glob('preparation_failure_*.json'))), 1)
                finally:
                    self.repo, self.session = original_repo, original_session


if __name__ == '__main__':
    unittest.main()
