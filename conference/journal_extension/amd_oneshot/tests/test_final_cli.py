"""Synthetic preparation/approval/build-overlay tests; no runtime is executed."""
import argparse
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import cli, distribution, protocol, runtime_fallback
from conference.journal_extension.amd_oneshot.common import load_json, save_json, sha256_file
from conference.journal_extension.amd_oneshot.tests import test_coordinator as coordinator_fixtures


class FinalCLITests(unittest.TestCase):
    def fixture(self, pending=False, approved=False):
        f = coordinator_fixtures.CoordinatorTests(methodName='test_full_72_and_completed_never_rerun')
        f.setUp()
        self.addCleanup(f.doCleanups)
        config = load_json(f.session / 'config.json')
        config.update(repo_root=str(f.session.parent), model=str(f.session / 'model'),
                      prompt=str(f.session / 'prompt'), session_started_wall_s=time.time())
        report = load_json(f.session / 'preflight.json')
        report.update(status='RUNTIME_FALLBACK_PENDING' if pending else 'PASS',
                      identities={name: {'sha256': f.plan[name + '_sha256']} for name in ('model', 'prompt')})
        approval = load_json(f.session / 'execution_authorized.json')
        approval['runtime_fallback_authorized'] = approved
        # Fixtures intentionally replace their own small synthetic documents.
        for name, value in [('config.json', config), ('preflight.json', report),
                            ('execution_authorized.json', approval)]:
            (f.session / name).write_text(__import__('json').dumps(value))
        return f, config, report, approval

    def test_derived_review_template_is_unsigned_and_binds_both_schedules(self):
        template = cli.review_template(package_hash='SYNTHETIC')
        self.assertIs(template['authorized'], False)
        self.assertIs(template['runtime_fallback_authorized'], False)
        self.assertEqual(set(template['schedule_sha256']), {'FULL', 'STATIC_SIGNAL'})
        with self.assertRaises(ValueError):
            protocol.check_approval(protocol.load_plan(), template, 'SYNTHETIC')

    def test_original_runtime_never_calls_builder(self):
        f, config, report, approval = self.fixture()
        with patch.object(runtime_fallback, 'build_fallback', side_effect=AssertionError('No build')):
            cli.prepare_runtime(f.session, f.plan, config, approval, report)
        self.assertTrue((f.session / 'session_clock.json').is_file())
        self.assertFalse((f.session / 'runtime_fallback_attempt.json').exists())

    def test_unapproved_fallback_is_rejected_before_builder(self):
        f, config, report, approval = self.fixture(pending=True)
        with patch.object(runtime_fallback, 'build_fallback', side_effect=AssertionError('No build')):
            with self.assertRaisesRegex(ValueError, 'not approved'):
                cli.prepare_runtime(f.session, f.plan, config, approval, report)

    def mocks(self, f, config):
        def hashes(path):
            for name in ('model', 'prompt'):
                if str(path) == config[name]:
                    return f.plan[name + '_sha256']
            return sha256_file(path)
        return patch.object(cli, 'sha256_file', side_effect=hashes)

    def test_approved_build_freezes_overlay_without_overwriting_originals(self):
        f, config, report, approval = self.fixture(pending=True, approved=True)
        before = {name: (f.session / name).read_bytes() for name in ('config.json', 'preflight.json')}
        manifest = {'synthetic': True, 'identity': 'NEW_PROSPECTIVE'}
        work = f.session.parent / ('.je_amd_runtime_' + f.session.name)
        save_json(work / 'runtime-fallback-manifest.json', manifest)
        built = {'config_overrides': {'server_bin': '/synthetic/new-server',
                    'expected_runtime_fallback_manifest_sha256': sha256_file(work / 'runtime-fallback-manifest.json')},
                 'manifest': manifest}
        with self.mocks(f, config), \
             patch.object(cli.preflight, 'check_machine_state', return_value={'status': 'PASS'}), \
             patch.object(runtime_fallback, 'build_fallback', return_value=built) as build, \
             patch.object(cli.preflight, 'perform_preflight', return_value={'status': 'PASS'}):
            cli.prepare_runtime(f.session, f.plan, config, approval, report)
            cli.prepare_runtime(f.session, f.plan, config, approval, report)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.args[3], config['session_started_wall_s'])
        self.assertEqual(load_json(f.session / 'runtime_selection.json')['status'], 'SELECTED')
        for name in before:
            self.assertEqual((f.session / name).read_bytes(), before[name])

    def test_failed_or_cancelled_build_keeps_logs_and_blocks_automatic_retry(self):
        for failure in (ValueError('synthetic failure'), KeyboardInterrupt('synthetic cancel')):
            with self.subTest(failure=type(failure).__name__):
                f, config, report, approval = self.fixture(pending=True, approved=True)
                def fail(config, approval, work, started):
                    work.mkdir()
                    (work / 'command-00.log').write_text('synthetic diagnostic')
                    save_json(work / 'runtime-fallback-failure.json', {'synthetic': True})
                    raise failure
                with self.mocks(f, config), \
                     patch.object(cli.preflight, 'check_machine_state', return_value={'status': 'PASS'}), \
                     patch.object(runtime_fallback, 'build_fallback', side_effect=fail) as build:
                    with self.assertRaises(type(failure)):
                        cli.prepare_runtime(f.session, f.plan, config, approval, report)
                    self.assertTrue((f.session / 'runtime_fallback_diagnostics/command-00.log').is_file())
                    with self.assertRaisesRegex(ValueError, 'Previous runtime fallback attempt'):
                        cli.prepare_runtime(f.session, f.plan, config, approval, report)
                self.assertEqual(build.call_count, 1)

    def test_run_build_cancellation_returns_verified_private_archive(self):
        f, config, report, approval = self.fixture(pending=True, approved=True)
        repo = f.session.parent
        target = repo / 'results/journal_extension_amd_synthetic'
        target.parent.mkdir()
        f.session.rename(target)
        f.session = target
        args = argparse.Namespace(confirm=True, session=f.session, approval=None)
        with patch.object(cli.distribution, 'CODE_ROOT', repo), \
             patch.object(cli, 'authorize_run', return_value=(f.plan, config, approval, report)), \
             patch.object(cli, 'prepare_runtime', side_effect=KeyboardInterrupt('synthetic cancel')), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                cli.do_run(args)
        archives = list(f.session.parent.glob('*private*.zip'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(cli.archive.validate_archive(archives[0])['status'], 'PASS')

    def test_preflight_defers_only_runtime_errors_and_never_builds(self):
        for errors, expected in [(['build provenance: ORIGINAL_BINARY_MISSING: synthetic'], 'RUNTIME_FALLBACK_PENDING'),
                                 (['build provenance: ORIGINAL_BINARY_MISSING: synthetic', 'battery'], 'NOT_READY'),
                                 (['build provenance: source changed'], 'NOT_READY')]:
            with self.subTest(errors=errors), tempfile.TemporaryDirectory() as name:
                repo = Path(name)
                recovered = repo / 'recovered'
                save_json(recovered / 'preflight/selected_topology.json', {'diagnostic_build': {'synthetic': True}})
                args = argparse.Namespace(repo=repo, session=None, historical_zip=None,
                                          server_bin=repo / 'missing-server', model=repo / 'model',
                                          prompt=None, build_manifest=None, build_manifest_sha256=None)
                report = {'status': 'NOT_READY', 'errors': errors, 'warnings': []}
                with patch.object(cli.distribution, 'CODE_ROOT', repo), \
                     patch.object(cli.git_delivery, 'verify_checkout', return_value={'commit_sha': 'a' * 40}), \
                     patch.object(cli.git_delivery, 'snapshot_source', return_value={'package_sha256': 'synthetic'}), \
                     patch.object(cli.historical_identity, 'stage_identity', return_value={'diagnostic_build': {'synthetic': True}}), \
                     patch.object(cli.historical_zip, 'ingest_archive', side_effect=AssertionError('Private history forbidden')) as ingest, \
                     patch.object(cli.preflight, 'perform_preflight', return_value=copy.deepcopy(report)), \
                     patch.object(runtime_fallback, 'build_fallback', side_effect=AssertionError('No build in preflight')), \
                     patch('subprocess.Popen', side_effect=AssertionError('No process in mock preflight')), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.do_preflight(args), 2)
                ingest.assert_not_called()
                self.assertEqual(load_json(repo / 'results/journal_extension_amd_01/preflight.json')['status'], expected)


if __name__ == '__main__':
    unittest.main()
