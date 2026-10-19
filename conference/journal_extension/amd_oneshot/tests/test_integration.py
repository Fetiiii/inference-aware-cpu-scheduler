"""Synthetic/local source-only validation. Never starts a model or real server."""
import contextlib
import copy
import io
import fcntl
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from conference.journal_extension.amd_oneshot import (
    analysis, archive, calibration, cli, distribution, historical, protocol,
)
from conference.journal_extension.amd_oneshot.common import digest, save_json


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.plan = protocol.load_plan()

    def test_complete_deterministic_rounds(self):
        slots = protocol.schedule(self.plan)
        self.assertEqual(len(slots), 72)
        self.assertEqual(slots, protocol.schedule(self.plan))
        self.assertEqual(len(set(r['run_id'] for r in slots)), 72)
        for stage in self.plan['cohorts']:
            for rd in range(1, stage['rounds'] + 1):
                group = [r for r in slots if r['cohort'] == stage['name'] and r['round'] == rd]
                self.assertEqual({r['arm'] for r in group}, set(self.plan[stage['arms'] + '_arms']))

    def test_fixed_worker_budgets_and_exact_masks(self):
        topo = {'masks': {'BIG4': [7, 13, 21, 29], 'COMPACT4': [30, 40, 50, 60],
                         'ALL_ALLOWED4': [7, 13, 21, 29, 30, 40, 50, 60, 70, 80, 90, 100],
                         'ALL_CORES12': [7, 13, 21, 29, 30, 40, 50, 60, 70, 80, 90, 100]}}
        static = protocol.static_specs(topo)
        self.assertEqual(static['ALL_CORES12']['threads'], 12)
        gate = {'status': 'ELIGIBLE', 'policy': {'initial_mask': 'ALL_ALLOWED4', 'final_mask': 'BIG4',
                'decode_workers': 4, 'prefill_workers': 4}, 'static_comparator': 'COMPACT4'}
        specs = protocol.policy_specs(topo, gate)
        self.assertTrue(all(s['threads'] == s['threads_batch'] == 4 for s in specs.values()))
        self.assertEqual(specs['EXTERNAL']['cpus'], specs['ORACLE']['cpus'])
        self.assertEqual(specs['EXTERNAL']['target_cpus'], specs['ORACLE']['target_cpus'])
        self.assertIsNone(specs['STOCK']['cpus'])
        gate['status'] = 'NOT_ELIGIBLE'
        with self.assertRaises(ValueError):
            protocol.policy_specs(topo, gate)

    def test_no_unreviewed_execution(self):
        with self.assertRaises(ValueError):
            protocol.check_approval(self.plan, {'authorized': False}, 'dummy')
        approval = {**protocol.approval_bindings(self.plan), 'authorized': True,
                    'author_review_id': 'SYNTHETIC', 'package_sha256': 'dummy',
                    'runtime_fallback_authorized': False}
        self.assertTrue(protocol.check_approval(self.plan, approval, 'dummy'))
        with self.assertRaises(ValueError):
            protocol.check_approval(self.plan, approval, 'different')

    def test_forecast_labels_loading_uncertainty(self):
        self.assertFalse(protocol.forecast(self.plan)['model_loading_measured'])
        records = [{**item, 'status': 'ok', 'startup_elapsed_s': 2, 'warmup_elapsed_s': 1, 'request_elapsed_s': 50}
                   for item in protocol.schedule(self.plan, 'technical_smoke')]
        forecast = protocol.forecast(self.plan, records)
        self.assertFalse(forecast['model_loading_measured'])
        self.assertTrue(forecast['startup_readiness_measured'])
        self.assertGreater(forecast['remaining_s'], 64 * 53)

    def test_zero_shot_amendment_or_duplicate_mask_refused(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-je-plan-') as name:
            changed = copy.deepcopy(self.plan)
            changed['zero_shot']['hi'] = 3001
            path = Path(name) / 'plan.json'
            save_json(path, changed)
            with self.assertRaises(ValueError):
                protocol.load_plan(path)
        with self.assertRaises(ValueError):
            protocol.static_specs({'BIG4': [1, 1, 2, 3], 'COMPACT4': [4, 5, 6, 7],
                                   'ALL_ALLOWED4': list(range(12)), 'ALL_CORES12': list(range(12))})


class AnchorTests(unittest.TestCase):
    def test_original_environment_required_not_defaults(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-je-anchor-') as name:
            root, plan = Path(name), protocol.load_plan()
            save_json(root / 'history/c03_00/summary.json', {'prompt': {'sha256': plan['prompt_sha256']}})
            self.assertEqual(historical.check_anchor(root, plan)['status'], 'NOT_READY')
            record = {'task': 'TASK-C03', 'selected_c03_path': 'CROSS_VENDOR',
                      'model': {'sha256': plan['model_sha256']}, 'prompt': {'sha256': plan['prompt_sha256']},
                      'llama_cpp': {'repository_commit': plan['source_revision'], 'diagnostic_server_binary': {'sha256': 'synthetic'}},
                      'protocol': {k: plan['runtime'][k] for k in ('ctx', 'batch', 'ubatch', 'n_predict', 'seed')}}
            record['protocol'].update(cooldown_s=30, initial_cooldown_s=30)
            path = root / 'history/c03_00/raw/environment/original.json'
            save_json(path, record)
            before = path.stat().st_mtime_ns
            self.assertEqual(historical.check_anchor(root, plan)['status'], 'PASS')
            self.assertEqual(path.stat().st_mtime_ns, before)
            record['prompt']['sha256'] = 'different'
            save_json(path.with_name('conflicting.json'), record)
            self.assertEqual(historical.check_anchor(root, plan)['status'], 'NOT_READY')


class NewAnalysisTests(unittest.TestCase):
    def records(self):
        rows = []
        for item in protocol.schedule(protocol.load_plan(), 'static_confirmatory'):
            offset = {'BIG4': 5, 'COMPACT4': 8, 'ALL_ALLOWED4': 6, 'ALL_CORES12': 4}[item['arm']]
            rows.append({**item, 'synthetic': True, 'status': 'ok', 'ttft_ms': 100 + offset * item['round'],
                         'itl_p95_ms': 90 + offset * item['round'], 'decode_tps': 10,
                         'samples': [], 'thermal_warning': item['run_id'].endswith('compact4') and item['round'] == 2})
        return rows

    def test_fixed_twelve_family_and_matched_six_rounds(self):
        rows, plan = self.records(), protocol.load_plan()
        report = analysis.analyze(rows, plan, {'status': 'NOT_ELIGIBLE'})
        self.assertTrue(report['synthetic'])
        tests = report['primary_comparisons']
        self.assertEqual(len(tests), 12)
        self.assertTrue(all(t['n'] == 6 for t in tests[:6]))
        self.assertTrue(all(t['multiplicity_bookkeeping_p'] == 1 and t['holm_p'] is None for t in tests[6:]))
        self.assertEqual(tests[0]['thermal_sensitivity']['n'], 5)
        # Missing ONE other arm means the complete-round design is incomplete.
        report = analysis.analyze(rows[1:], plan, {'status': 'NOT_ELIGIBLE'})
        self.assertTrue(all(t['status'] == 'NOT_AVAILABLE' for t in report['primary_comparisons']))

    def test_duplicate_or_synthetic_matching_forbidden(self):
        rows = self.records()
        with self.assertRaises(ValueError):
            analysis.analyze(rows + rows[:1], protocol.load_plan(), {})

    def test_outputs_exclusive(self):
        rows, plan = self.records(), protocol.load_plan()
        result = analysis.analyze(rows, plan, {'status': 'NOT_ELIGIBLE'})
        with tempfile.TemporaryDirectory(prefix='synthetic-je-analysis-') as name:
            analysis.write_outputs(rows, result, name)
            self.assertIn('UNKNOWN', (Path(name) / 'summary.md').read_text())
            with self.assertRaises(FileExistsError):
                analysis.write_outputs(rows, result, name)


class DistributionAndCLITests(unittest.TestCase):
    def test_portable_launcher_ignores_caller_shadow_code(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-shadow-repository-') as name:
            root = Path(name)
            (root / 'conference').mkdir()
            (root / 'conference/__init__.py').write_text('raise RuntimeError("MUST NOT IMPORT COLLABORATOR CODE")\n')
            launcher = distribution.CODE_ROOT / distribution.PACKAGE / 'amd_oneshot.sh'
            result = subprocess.run(['bash', str(launcher), '--help'], cwd=root,
                                    env={**os.environ, 'PYTHONPATH': str(root)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('preflight', result.stdout)

    def test_resume_pause_does_not_reset_wall_deadline(self):
        from conference.journal_extension.amd_oneshot.tests.test_coordinator import CoordinatorTests
        from conference.journal_extension.amd_oneshot.common import ledger
        fixture = CoordinatorTests(methodName='test_full_72_and_completed_never_rerun')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        sleeps = []
        def interrupt(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt()
            fixture.sleep(seconds)
        fixture.deps['sleep'] = interrupt
        self.assertEqual(fixture.run_session()['status'], 'STOPPED')
        self.assertEqual(len(fixture.calls), 1)
        first = next(e['timestamp_ns'] for e in ledger(fixture.session) if e['kind'] == 'session_begin')
        fixture.deps.update(sleep=fixture.sleep, wall_clock=lambda: first + 20_000 * 10**9)
        result = fixture.run_session()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertIn('budget', result['reason'])
        self.assertEqual(len(fixture.calls), 1)

    def test_historical_paths_are_not_current_runtime_guards(self):
        from conference.journal_extension.amd_oneshot import coordinator
        from conference.journal_extension.amd_oneshot.common import sha256_file
        with tempfile.TemporaryDirectory(prefix='synthetic-provenance-separation-') as name:
            root = Path(name)
            paths = {key: root / key for key in ('model', 'prompt', 'server', 'library')}
            for key, path in paths.items():
                path.write_text('synthetic-' + key)
            ids = {k: {'resolved_path': str(p), 'sha256': sha256_file(p)} for k, p in paths.items()}
            build = {'binary': ids['server'], 'shared_libraries': [ids['library']],
                     'historical_known_identities': {'server_binary': {'resolved_path': '/nonexistent/redacted/historical-server', 'sha256': 'old'}}}
            report = {'identities': ids, 'source_fingerprint': build}
            with patch.object(coordinator.preflight, 'verify_build', return_value=build):
                guards = coordinator._verify_identities({k: str(p) for k, p in paths.items()}, report)
            self.assertIn(str(paths['library']), guards)
            self.assertNotIn('/nonexistent/redacted/historical-server', guards)

    def test_injected_loader_hooks_stop_before_any_tool(self):
        from conference.journal_extension.amd_oneshot import preflight
        with patch.dict('os.environ', {'LD_AUDIT': '/unapproved/library.so'}), \
             patch.object(preflight.c03, 'run_readonly', side_effect=AssertionError('No loader/tool permitted')):
            with self.assertRaisesRegex(ValueError, 'RUNTIME_LOADER_OVERRIDE'):
                preflight._runtime_closure(Path('/synthetic/server'))

    def test_runtime_temperature_uses_portable_amd_sensor(self):
        from conference.journal_extension.amd_oneshot import runtime
        with patch.object(runtime, 'collect_thermal_state', return_value={'selected_c': 61.5}), \
             patch.object(runtime.bl, 'package_temp_c', side_effect=AssertionError('Intel-only thermal helper forbidden')):
            self.assertEqual(runtime.package_temperature(), 61.5)

    def test_next_request_cannot_consume_archival_reserve(self):
        from conference.journal_extension.amd_oneshot.tests.test_coordinator import CoordinatorTests
        fixture = CoordinatorTests(methodName='test_full_72_and_completed_never_rerun')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.deps['sleep'] = lambda seconds: setattr(fixture, 'now', 10000)
        result = fixture.run_session()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertIn('budget', result['reason'])
        self.assertFalse(fixture.calls)

    def test_cli_error_archive_respects_live_session_lock(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-je-lock-') as name:
            session = Path(name) / 'results/journal_extension_amd_synthetic'
            session.mkdir(parents=True)
            with (session / '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(cli.distribution, 'CODE_ROOT', Path(name)), \
                     patch.object(cli, 'final_archive', side_effect=AssertionError('Cannot archive live session')), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(['run', '--session', str(session), '--confirm']), 2)

    def test_no_automatic_install_or_configuration_mutation_commands(self):
        forbidden = ['sudo ', 'pip install', 'apt install', 'pacman -S',
                     'npm install', 'sysctl -w', 'scxctl start', 'wget ', 'curl ']
        for path in (distribution.CODE_ROOT / distribution.PACKAGE).iterdir():
            if path.suffix in ('.py', '.sh'):
                text = path.read_text()
                for token in forbidden:
                    self.assertNotIn(token, text, (path.name, token))

    def test_mock_full_session_real_calibration_analysis_and_archive(self):
        self.real_session()

    def test_mock_negative_validation_finishes_static_48_without_retuning(self):
        self.real_session(failed_validation=True)

    def real_session(self, failed_validation=False):
        # Lifecycle fixture is synthetic; only its request backend is simulated.
        from conference.journal_extension.amd_oneshot.tests.test_coordinator import CoordinatorTests
        fixture = CoordinatorTests(methodName='test_full_72_and_completed_never_rerun')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fit_calls = []
        def fit_once(records, spec):
            fit_calls.append([r['run_id'] for r in records])
            return calibration.fit(records, spec)
        def backend(config, item, spec, target, model):
            record = fixture.backend(config, item, spec, target, model)
            ttft, itl = {'BIG4': (2000, 90), 'COMPACT4': (3000, 250),
                         'ALL_ALLOWED4': (1000, 200), 'ALL_CORES12': (800, 80)}.get(item['arm'], (1100, 95))
            falling = item['cohort'] == 'static_confirmatory' and item['arm'] == 'COMPACT4'
            record.update(ttft_ms=ttft + item['round'], itl_p95_ms=itl + item['round'] / 10,
                          itl_p50_ms=itl - 5, itl_p99_ms=itl + 5, decode_tps=10,
                          n_tokens=256, threads=spec['threads'], threads_batch=spec['threads_batch'],
                          t_internal_phase_ns=None if failed_validation and item['cohort'] == 'validation' else 500_000_000,
                          thermal_warning=False,
                          samples=[{'t_ns': i * 20_000_000,
                                    'norm_ctx_per_cpu_s': (4000 if (i < 25) == falling else 100) + (i % 5)} for i in range(50)])
            return record
        fixture.deps.update(backend=backend, fit=fit_once, evaluate=calibration.evaluate,
                            select_opportunity=calibration.select_opportunity,
                            transfer_report=calibration.transfer_report,
                            gate_policy=calibration.gate_policy, analyzer=analysis.analyze,
                            output_writer=analysis.write_outputs, archive_builder=archive.archive_session)
        result = fixture.run_session()
        self.assertEqual(result['status'], 'COMPLETE', result)
        expected_requests = 48 if failed_validation else 72
        self.assertEqual(len(fixture.calls), expected_requests)
        self.assertEqual(len(fit_calls), 1)
        self.assertEqual(len(fit_calls[0]), 6)
        transfer = json.loads((fixture.session / 'frozen_transfer_report.json').read_text())['report']
        self.assertEqual(transfer['reports']['ALL_ALLOWED4']['matched_requests'], 6)
        self.assertEqual(transfer['reports']['COMPACT4']['matched_requests'], 0)
        self.assertEqual(transfer['reports']['COMPACT4']['domain'], 'other_four_worker_placement')
        self.assertFalse(transfer['retuning_allowed'])
        verified = archive.validate_archive(Path(result['final_archive']['archive']))
        self.assertEqual(verified['status'], 'PASS', verified)
        self.assertTrue(verified['external_archive_hash_checked'])
        with zipfile.ZipFile(result['final_archive']['archive']) as z:
            self.assertEqual(sum(n.endswith('/runs.csv') for n in z.namelist()), 1)
            summaries = [n for n in z.namelist() if n.endswith('/analysis.json')]
            report = json.loads(z.read(summaries[0]))
            self.assertTrue(report['synthetic'])
            self.assertEqual(report['valid_requests'], expected_requests)
            self.assertEqual(sum(x['status'] == 'COMPLETE' for x in report['primary_comparisons']),
                             6 if failed_validation else 12)

    def test_source_bundle_crc_sha_and_portable_dependencies(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-je-prep-') as name:
            path = Path(name) / 'prep.zip'
            report = distribution.build_prep_zip(path)
            self.assertEqual(report['status'], 'PASS')
            self.assertEqual(distribution.validate_prep_zip(path), report)
            with zipfile.ZipFile(path) as z:
                self.assertIn('amd_oneshot.sh', z.namelist())
                self.assertIn('harness/phase_switch.py', z.namelist())
                template = json.loads(z.read('execution_approval.template.json'))
                self.assertIs(template['authorized'], False)
                self.assertIs(template['runtime_fallback_authorized'], False)
                self.assertEqual(template['package_sha256'], distribution.package_fingerprint())
                self.assertEqual(template['schedule_sha256'], protocol.approval_bindings(protocol.load_plan())['schedule_sha256'])
                self.assertFalse(any(n.endswith(('.gguf', '.so', '.bin')) for n in z.namelist()))
            with self.assertRaises(FileExistsError):
                distribution.build_prep_zip(path)

    def test_run_without_confirm_or_preflight_never_invokes_backend(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-je-cli-') as name:
            with patch('subprocess.Popen', side_effect=AssertionError('No process allowed')), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['run', '--session', name]), 2)
                self.assertEqual(cli.main(['run', '--session', str(Path(name) / 'missing'), '--confirm']), 2)

    def test_preflight_collects_first_archives_on_gate_failure_without_inference(self):
        calls = []
        with tempfile.TemporaryDirectory(prefix='synthetic-je-cli-') as name:
            repo = Path(name); (repo / 'results').mkdir()
            recovered = repo / 'synthetic-recovered'
            save_json(recovered / 'preflight/selected_topology.json', {'diagnostic_build': {'synthetic': True}})
            report = {'status': 'NOT_READY', 'errors': ['model missing'], 'warnings': []}
            received = []
            def machine_preflight(config, destination):
                calls.append('preflight'); received.append(config)
                return copy.deepcopy(report)
            with patch.object(cli.distribution, 'CODE_ROOT', repo), \
                 patch.object(cli.git_delivery, 'verify_checkout', side_effect=lambda *a: calls.append('git') or {'commit_sha': 'a' * 40}), \
                 patch.object(cli.git_delivery, 'snapshot_source', side_effect=lambda *a, **kw: calls.append('source') or {'package_sha256': 'synthetic'}), \
                 patch.object(cli.historical_zip, 'ingest_archive', side_effect=AssertionError('Private history forbidden')) as ingest, \
                 patch.object(cli.historical_identity, 'stage_identity', side_effect=lambda *a: calls.append('identity') or {'diagnostic_build': {'synthetic': True}}), \
                 patch.object(cli.preflight, 'perform_preflight', side_effect=machine_preflight), \
                 patch('subprocess.Popen', side_effect=AssertionError('No process allowed')), contextlib.redirect_stdout(io.StringIO()):
                status = cli.main(['preflight', '--repo', str(repo), '--server-bin', str(repo / 'missing-server'), '--model', str(repo / 'missing-model')])
            self.assertEqual(status, 2)
            ingest.assert_not_called()
            self.assertEqual(calls, ['git', 'source', 'identity', 'preflight'])
            self.assertEqual(received[0]['historical_identity'], {'synthetic': True})
            self.assertEqual(len(list((repo / 'results').glob('*private*.zip'))), 1)


if __name__ == '__main__':
    unittest.main()
