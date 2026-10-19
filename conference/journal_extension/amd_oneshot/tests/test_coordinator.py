"""Package-local synthetic lifecycle tests; no actual inference or real sleeps."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import coordinator, protocol, runtime
from conference.journal_extension.amd_oneshot.common import append_event, digest, ledger, load_json, save_json, sha256_file


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-amd-coordinator-')
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name) / 'synthetic-session'
        self.session.mkdir()
        self.plan = protocol.load_plan()
        save_json(self.session / 'source' / 'synthetic.json', {'synthetic': True})
        files = {'synthetic.json': sha256_file(self.session / 'source' / 'synthetic.json')}
        self.package = digest(files)
        save_json(self.session / 'source_manifest.json', {'package_sha256': self.package, 'files': files})
        save_json(self.session / 'historical_c03' / 'synthetic.json', {'synthetic': True})
        history_files = [{'path': 'synthetic.json', 'sha256': sha256_file(self.session / 'historical_c03' / 'synthetic.json')}]
        save_json(self.session / 'historical_collection.json', {'inventory': history_files})
        save_json(self.session / 'historical_c03' / 'history_inventory.json', {'inventory': history_files})
        (self.session / 'plan.json').write_text(json.dumps(self.plan))
        save_json(self.session / 'config.json', {'plan': self.plan, 'synthetic': True,
                  'package_sha256': self.package, 'interval_ms': 20,
                  'pinned_revision': self.plan['source_revision'],
                  'expected_model_sha256': self.plan['model_sha256'],
                  'expected_prompt_sha256': self.plan['prompt_sha256'],
                  **self.plan['runtime'], **self.plan['safety']})
        masks = {'BIG4': list(range(4)), 'COMPACT4': list(range(4, 8)),
                 'ALL_ALLOWED4': list(range(12)), 'ALL_CORES12': list(range(12))}
        save_json(self.session / 'preflight.json', {'status': 'PASS', 'topology': masks,
                                                  'machine_state': {}, 'synthetic': True})
        save_json(self.session / 'historical_identity.json', {'status': 'PASS', 'synthetic': True, 'records': history_files})
        save_json(self.session / 'execution_authorized.json', {
            'authorized': True, 'author_review_id': 'SYNTHETIC-ONLY',
            'plan_sha256': digest(self.plan), 'package_sha256': self.package,
            'selected_tier': self.plan.get('selected_tier', 'FULL'),
            'maximum_session_s': self.plan['maximum_session_s'],
            **protocol.approval_bindings(self.plan)})
        self.now = 0
        self.calls = []
        self.archives = []
        self.evaluations = []
        self.fit_ids = []
        self.deps = {'clock': lambda: self.now, 'sleep': self.sleep,
                     'package_hash': lambda config: self.package,
                     'identity_check': lambda config, report: {},
                     'machine_check': lambda config, expected: {'status': 'PASS'},
                     'backend': self.backend, 'fit': self.fit, 'evaluate': self.evaluate,
                     'gate_policy': self.gate, 'archive_builder': self.archive,
                     'select_opportunity': self.select,
                     'transfer_report': self.transfer,
                     'analyzer': lambda records, plan, gate: {'synthetic': True, 'n': len(records)},
                     'output_writer': lambda records, result, destination: None}

    def sleep(self, seconds):
        self.now += seconds

    def backend(self, config, item, spec, attempt, model):
        self.calls.append(deepcopy(item))
        attempt.mkdir()
        save_json(attempt / 'synthetic_raw.json', {'synthetic': True, 'run_id': item['run_id']})
        self.now += 3
        return {**item, 'status': 'ok', 'synthetic': True,
                'startup_elapsed_s': 1, 'warmup_elapsed_s': 1, 'request_elapsed_s': 1}

    def fit(self, records, spec):
        self.fit_ids = [r['run_id'] for r in records]
        self.assertEqual(len(records), 6)
        self.assertTrue(all(r['cohort'] == 'calibration' for r in records))
        self.assertTrue(all(r['operating_configuration'] == 'ALL_ALLOWED4' for r in records))
        self.assertTrue((self.session / 'selection.json').exists())
        return {'synthetic': True, 'mode': 'calibrated'}

    def evaluate(self, records, model, spec):
        self.assertEqual(len(records), 6)
        self.assertTrue((self.session / 'frozen_detector.json').exists())
        self.assertTrue(all(r['cohort'] == 'validation' for r in records))
        self.evaluations.append(model['mode'])
        return {'synthetic': True}

    def select(self, exploration, spec):
        self.assertEqual(len(exploration), 8)
        self.assertTrue(all(r['cohort'] == 'exploration' for r in exploration))
        return {'status': 'OPPORTUNITY', 'selection_hash': digest(exploration),
                'initial_mask': 'ALL_ALLOWED4'}

    def transfer(self, records, model, spec):
        self.assertEqual(len(records), 24)
        self.assertTrue((self.session / 'frozen_detector.json').exists())
        return {'synthetic': True, 'retuning_allowed': False}

    def gate(self, exploration, report, model, spec):
        self.assertEqual(len(exploration), 8)
        self.assertTrue(all(r['cohort'] == 'exploration' for r in exploration))
        return {'status': 'ELIGIBLE', 'selection_hash': digest(exploration),
                'policy': {'initial_mask': 'ALL_ALLOWED4',
                'final_mask': 'BIG4', 'prefill_workers': 4, 'decode_workers': 4},
                'static_comparator': 'BIG4'}

    def archive(self, session, target):
        self.assertFalse(target.exists())
        self.assertFalse(target.is_relative_to(session))
        self.archives.append(target)
        return {'status': 'PASS', 'archive': str(target), 'synthetic': True}

    def run_session(self):
        return coordinator.execute_session(self.session, self.deps)

    def test_full_72_and_completed_never_rerun(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 72)
        self.assertEqual(self.evaluations, ['zero_shot', 'calibrated'])
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 72)
        self.assertEqual(len(set(self.archives)), 2)

    def test_negative_fit_still_zero_shot_and_static(self):
        def no_fit(records, spec):
            raise ValueError('no nondegenerate train-only threshold candidates')
        self.deps['fit'] = no_fit
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 48)
        self.assertEqual(self.evaluations, ['zero_shot'])
        skipped = [e for e in ledger(self.session) if e['kind'] == 'cohort_skipped']
        self.assertEqual(len(skipped[0]['run_ids']), 24)

    def test_static_signal_tier_is_frozen_from_four_smoke_timings(self):
        def slow_smoke(*args):
            record = self.backend(*args)
            if record['cohort'] == 'technical_smoke':
                record['startup_elapsed_s'] = 100
            return record
        self.deps['backend'] = slow_smoke
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 48)
        self.assertFalse(any(r['cohort'] == 'policy_confirmatory' for r in self.calls))
        skips = [e for e in ledger(self.session) if e['kind'] == 'cohort_skipped']
        self.assertTrue(any('NOT_PLANNED' in e['reason'] for e in skips))
        self.assertEqual(load_json(self.session / 'tier_decision.json')['selected_tier'], 'STATIC_SIGNAL')
        self.assertEqual(load_json(self.session / 'plan.json')['selected_tier'], 'AUTO')

    def test_selection_and_transfer_are_frozen_with_inputs(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        selection = load_json(self.session / 'selection.json')
        self.assertTrue(selection['inputs_digest'])
        transfer = load_json(self.session / 'frozen_transfer_report.json')
        self.assertTrue(transfer['inputs_digest'])
        self.assertFalse(transfer['report']['retuning_allowed'])
        events = ledger(self.session)
        selected_at = next(i for i, e in enumerate(events) if e['kind'] == 'selection_frozen')
        trained_at = next(i for i, e in enumerate(events) if e['kind'] == 'attempt_begin'
                          and 'calibration' in e['run_id'])
        self.assertLess(selected_at, trained_at)

    def test_no_opportunity_still_observes_fixed_fallback(self):
        self.deps['select_opportunity'] = lambda exploration, spec: {
            'status': 'NO_OPPORTUNITY', 'initial_mask': 'ALL_ALLOWED4',
            'selection_hash': digest(exploration), 'reasons': ['IDENTICAL_ENDPOINTS']}
        self.deps['gate_policy'] = lambda exploration, *args: {
            'status': 'NOT_ELIGIBLE', 'selection_hash': digest(exploration),
            'reasons': ['IDENTICAL_ENDPOINTS']}
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 48)
        self.assertEqual(len(self.fit_ids), 6)
        observed = [r for r in self.calls if r['cohort'] in ('calibration', 'validation')]
        self.assertEqual(len(observed), 12)
        self.assertEqual({r['operating_configuration'] for r in observed}, {'ALL_ALLOWED4'})

    def test_session_cannot_change_tier_without_new_package(self):
        plan = load_json(self.session / 'plan.json')
        plan['selected_tier'] = 'STATIC_SIGNAL'
        (self.session / 'plan.json').write_text(json.dumps(plan))
        self.assertIn('Unapproved protocol revision', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_gate_cannot_substitute_selection(self):
        self.deps['gate_policy'] = lambda *args: {'status': 'ELIGIBLE', 'selection_hash': 'changed'}
        self.assertIn('selection differs', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 24)

    def test_selection_tampering_blocks_resume(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        path = self.session / 'selection.json'
        value = load_json(path)
        value['selection']['initial_mask'] = 'BIG4'
        path.write_text(json.dumps(value))
        self.assertIn('selection digest changed', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 72)

    def test_unexpected_fit_error_is_not_negative_result(self):
        self.deps['fit'] = lambda *args: (_ for _ in ()).throw(ValueError('bug'))
        result = self.run_session()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(len(self.calls), 18)
        self.assertEqual(len(self.archives), 1)
        self.assertFalse((self.session / 'frozen_detector.json').exists())

    def test_one_retry_per_slot(self):
        count = []
        def fail(*args):
            count.append(1)
            raise runtime.RuntimeFailure('startup_timeout', retryable=True)
        self.deps['backend'] = fail
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(len(count), 2)
        self.run_session()
        self.assertEqual(len(count), 2)

    def test_global_four_retry_limit(self):
        seen = set()
        def backend(config, item, spec, attempt, model):
            if item['run_id'] not in seen:
                seen.add(item['run_id'])
                raise runtime.RuntimeFailure('connection_before_tokens', retryable=True)
            return self.backend(config, item, spec, attempt, model)
        self.deps['backend'] = backend
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sum(e['kind'] == 'retry_authorized' for e in ledger(self.session)), 4)

    def test_after_token_failure_cannot_retry(self):
        self.deps['backend'] = lambda *args: (_ for _ in ()).throw(
            runtime.RuntimeFailure('connection_before_tokens', retryable=True, tokens_seen=1))
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(sum(e['kind'] == 'attempt_begin' for e in ledger(self.session)), 1)

    def test_between_request_interrupt_resumes_without_duplicate(self):
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt()
            self.sleep(seconds)
        self.deps['sleep'] = sleep
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(len(self.calls), 1)
        self.deps['sleep'] = self.sleep
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 72)

    def test_inflight_interrupt_blocks_resume(self):
        self.deps['backend'] = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_session()
        self.deps['backend'] = self.backend
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(self.calls, [])

    def test_unfinished_attempt_refused_and_archived(self):
        append_event(self.session, 'attempt_begin', run_id='synthetic', attempt_id='synthetic')
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.archives), 1)

    def test_duplicate_completed_refused(self):
        for _ in range(2):
            append_event(self.session, 'request_completed', run_id='duplicate', attempt_id='x')
        self.assertIn('duplicate', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_machine_gate_failure_archives(self):
        self.deps['machine_check'] = lambda *args, **kwargs: {'status': 'NOT_READY', 'errors': ['synthetic']}
        result = self.run_session()
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['final_archive']['status'], 'PASS')
        self.assertEqual(self.calls, [])

    def test_large_smoke_forecast_stops_without_shrinking(self):
        def costly(*args):
            record = self.backend(*args)
            record['startup_elapsed_s'] = 500
            return record
        self.deps['backend'] = costly
        self.assertIn('forecast exceeds', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(load_json(self.session / 'tier_decision.json')['selected_tier'], 'STOP')

    def test_duration_decision_is_frozen_before_exploration(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        events = ledger(self.session)
        decided = next(i for i, event in enumerate(events) if event['kind'] == 'tier_decision_frozen')
        explored = next(i for i, event in enumerate(events) if event['kind'] == 'attempt_begin'
                        and 'exploration' in event['run_id'])
        self.assertLess(decided, explored)
        self.assertEqual(sum(event['kind'] == 'request_completed' for event in events[:decided]), 4)
        decision = load_json(self.session / 'tier_decision.json')
        self.assertEqual(decision['selected_tier'], 'FULL')
        self.assertEqual(decision['schedule_sha256'], protocol.approval_bindings(self.plan)['schedule_sha256'])

    def test_duration_decision_tampering_blocks_resume(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        path = self.session / 'tier_decision.json'
        value = load_json(path)
        value['selected_tier'] = 'STATIC_SIGNAL'
        path.write_text(json.dumps(value))
        self.assertIn('duration decision digest changed', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 72)

    def test_missing_frozen_duration_file_cannot_be_recomputed(self):
        def interrupted_sleep(seconds):
            self.sleep(seconds)
            if len(self.calls) == 5:
                raise KeyboardInterrupt()
        self.deps['sleep'] = interrupted_sleep
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        path = self.session / 'tier_decision.json'
        self.assertTrue(path.exists())
        path.unlink()
        self.deps['sleep'] = self.sleep
        self.assertIn('recomputation forbidden', self.run_session()['reason'])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(sum(event['kind'] == 'tier_decision_frozen' for event in ledger(self.session)), 1)

    def test_resume_uses_original_duration_inputs_without_reselection(self):
        def interrupted_sleep(seconds):
            self.sleep(seconds)
            if len(self.calls) == 5:
                raise KeyboardInterrupt()
        self.deps['sleep'] = interrupted_sleep
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        path = self.session / 'tier_decision.json'
        original = load_json(path)
        self.assertEqual(original['selected_tier'], 'FULL')
        original_hash = sha256_file(path)
        smoke = [load_json(self.session / event['record']) for event in ledger(self.session)
                 if event['kind'] == 'request_completed' and 'technical_smoke' in event['run_id']]
        self.assertEqual(protocol.duration_decision(self.plan, smoke, 8000)['selected_tier'], 'STATIC_SIGNAL')
        self.deps['sleep'] = self.sleep
        self.deps['wall_clock'] = lambda: time.time_ns() + 8000 * 10**9
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(sha256_file(path), original_hash)
        self.assertEqual(load_json(path), original)
        self.assertEqual(sum(event['kind'] == 'tier_decision_frozen' for event in ledger(self.session)), 1)
        self.assertEqual(len(self.calls), 72)

    def test_session_clock_includes_setup_build_in_duration_choice(self):
        baseline = time.time_ns()
        save_json(self.session / 'session_clock.json', {
            'session_started_wall_s': baseline / 1e9 - 8000,
            'approval_sha256': sha256_file(self.session / 'execution_authorized.json')})
        self.deps['wall_clock'] = lambda: baseline + int(self.now * 1e9)
        checked = []
        def machine(*args, **kwargs):
            checked.append(1)
            return {'status': 'PASS' if len(checked) <= 4 else 'NOT_READY'}
        self.deps['machine_check'] = machine
        self.assertEqual(self.run_session()['status'], 'STOPPED')
        self.assertEqual(len(self.calls), 4)
        decision = load_json(self.session / 'tier_decision.json')
        self.assertGreaterEqual(decision['inputs']['elapsed_s'], 8000)
        self.assertEqual(decision['selected_tier'], 'STATIC_SIGNAL')

    def test_session_clock_is_bound_to_approval(self):
        save_json(self.session / 'session_clock.json', {
            'session_started_wall_s': time.time() - 10, 'approval_sha256': 'wrong'})
        self.assertIn('clock approval mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_session_clock_cannot_change_on_resume(self):
        save_json(self.session / 'session_clock.json', {
            'session_started_wall_s': time.time() - 10,
            'approval_sha256': sha256_file(self.session / 'execution_authorized.json')})
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        path = self.session / 'session_clock.json'
        value = load_json(path)
        value['session_started_wall_s'] += 1
        path.write_text(json.dumps(value))
        self.assertIn('inputs changed on resume', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 72)

    def prepare_runtime_overlay(self, authorized=True):
        original_report = load_json(self.session / 'preflight.json')
        original_report['status'] = 'RUNTIME_FALLBACK_PENDING'
        (self.session / 'preflight.json').write_text(json.dumps(original_report))
        approval = load_json(self.session / 'execution_authorized.json')
        approval['runtime_fallback_authorized'] = authorized
        (self.session / 'execution_authorized.json').write_text(json.dumps(approval))
        config = load_json(self.session / 'config.json')
        config['synthetic_overlay'] = True
        source_build = Path(self.temp.name) / 'authoritative_build.json'
        save_json(source_build, {'synthetic_build': True})
        save_json(self.session / 'runtime_fallback_build.json', load_json(source_build))
        config['runtime_fallback_manifest'] = str(source_build)
        config['expected_runtime_fallback_manifest_sha256'] = sha256_file(source_build)
        save_json(self.session / 'runtime_fallback_diagnostics' / 'configure.json', {'synthetic_log': True})
        save_json(self.session / 'execution_config.json', config)
        save_json(self.session / 'execution_preflight.json', {**original_report, 'status': 'PASS'})
        names = {'original_config_sha256': 'config.json', 'original_preflight_sha256': 'preflight.json',
                 'execution_config_sha256': 'execution_config.json', 'execution_preflight_sha256': 'execution_preflight.json',
                 'approval_sha256': 'execution_authorized.json'}
        save_json(self.session / 'runtime_selection.json', {
            'status': 'SELECTED', **{key: sha256_file(self.session / name) for key, name in names.items()},
            'runtime_build_sha256': sha256_file(source_build),
            'diagnostics_sha256': {'configure.json': sha256_file(self.session / 'runtime_fallback_diagnostics' / 'configure.json')},
            'runtime_fallback_policy_sha256': digest(self.plan['runtime_fallback_policy'])})

    def test_authorized_runtime_overlay_selected_without_replacing_originals(self):
        self.prepare_runtime_overlay()
        def identities(config, report):
            self.assertTrue(config['synthetic_overlay'])
            self.assertEqual(report['status'], 'PASS')
            return {}
        self.deps['identity_check'] = identities
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(load_json(self.session / 'preflight.json')['status'], 'RUNTIME_FALLBACK_PENDING')
        self.assertNotIn('synthetic_overlay', load_json(self.session / 'config.json'))

    def test_unauthorized_runtime_overlay_cannot_execute(self):
        self.prepare_runtime_overlay(authorized=False)
        self.assertIn('unauthorized runtime execution overlay', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_runtime_overlay_hash_change_cannot_execute(self):
        self.prepare_runtime_overlay()
        path = self.session / 'execution_config.json'
        value = load_json(path)
        value['synthetic_overlay'] = 'changed'
        path.write_text(json.dumps(value))
        self.assertIn('runtime execution overlay', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_archived_runtime_build_manifest_tampering_cannot_execute(self):
        self.prepare_runtime_overlay()
        (self.session / 'runtime_fallback_build.json').write_text('{"changed": true}')
        self.assertIn('archived build manifest identity mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_authoritative_runtime_build_manifest_tampering_cannot_execute(self):
        self.prepare_runtime_overlay()
        source = Path(load_json(self.session / 'execution_config.json')['runtime_fallback_manifest'])
        source.write_text('{"changed": true}')
        self.assertIn('evidence manifest hash mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_archived_runtime_diagnostics_tampering_cannot_execute(self):
        self.prepare_runtime_overlay()
        (self.session / 'runtime_fallback_diagnostics' / 'configure.json').write_text('{"changed": true}')
        self.assertIn('source snapshot file census/hash mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_extra_archived_runtime_diagnostics_cannot_execute(self):
        self.prepare_runtime_overlay()
        save_json(self.session / 'runtime_fallback_diagnostics' / 'extra.json', {'unexpected': True})
        self.assertIn('source snapshot file census/hash mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_archived_build_and_diagnostics_remain_guarded_between_requests(self):
        self.prepare_runtime_overlay()
        def mutating_backend(*args):
            result = self.backend(*args)
            if len(self.calls) == 1:
                (self.session / 'runtime_fallback_build.json').write_text('{"changed": true}')
            return result
        self.deps['backend'] = mutating_backend
        self.assertIn('frozen input drift', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 1)

    def test_unbound_overlay_cannot_execute(self):
        save_json(self.session / 'execution_config.json', load_json(self.session / 'config.json'))
        self.assertIn('unbound runtime execution overlay', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_archive_failure_not_success(self):
        self.deps['archive_builder'] = lambda *args: {'status': 'FAIL'}
        self.assertEqual(self.run_session()['status'], 'ARCHIVE_FAILED')

    def test_negative_validation_gate_keeps_static_confirmatory(self):
        self.deps['gate_policy'] = lambda exploration, *args: {'status': 'NOT_ELIGIBLE',
            'selection_hash': digest(exploration), 'reasons': ['synthetic gate']}
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        self.assertEqual(len(self.calls), 48)
        self.assertEqual(sum(r['cohort'] == 'static_confirmatory' for r in self.calls), 24)

    def test_every_request_has_machine_gate(self):
        checks = []
        def machine(config, expected):
            checks.append(1)
            return {'status': 'PASS'}
        self.deps['machine_check'] = machine
        self.run_session()
        self.assertEqual(len(checks), 72)

    def test_elapsed_previous_segments_is_budgeted(self):
        frozen = digest({name: load_json(self.session / name) for name in
                         ('config.json', 'plan.json', 'preflight.json', 'execution_authorized.json',
                          'historical_identity.json', 'source_manifest.json', 'historical_collection.json',
                          'historical_c03/history_inventory.json')})
        append_event(self.session, 'session_begin', segment='prior-synthetic', frozen_digest=frozen)
        append_event(self.session, 'session_end', segment='prior-synthetic', elapsed_s=10200)
        self.assertIn('budget', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_unclosed_segment_refused(self):
        append_event(self.session, 'session_begin', segment='synthetic-crash', frozen_digest='x')
        self.assertIn('unclosed', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_approval_package_pin_is_independently_checked(self):
        self.deps['package_hash'] = lambda config: 'different-synthetic-package'
        self.assertIn('Approval does not match', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_tampered_flattened_runtime_config_refused(self):
        path = self.session / 'config.json'
        config = load_json(path)
        config['n_predict'] += 1
        path.write_text(json.dumps(config))
        self.assertIn('runtime/safety differs', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_nonpackaged_plan_refused_even_with_matching_approval(self):
        path = self.session / 'plan.json'
        plan = load_json(path)
        plan['runtime']['seed'] += 1
        path.write_text(json.dumps(plan))
        self.assertIn('packaged frozen plan', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_raw_snapshot_pinned_and_tampering_blocks_resume(self):
        self.assertEqual(self.run_session()['status'], 'COMPLETE')
        event = next(e for e in ledger(self.session) if e['kind'] == 'request_completed')
        self.assertIn('synthetic_raw.json', event['raw_sha256'])
        path = self.session / 'attempts' / event['attempt_id'] / 'synthetic_raw.json'
        path.write_text('{"synthetic": "tampered"}')
        self.assertIn('raw attempt artifact digest changed', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 72)

    def test_unledgered_attempt_directory_blocks_resume(self):
        (self.session / 'attempts' / 'synthetic-unledgered').mkdir(parents=True)
        self.assertIn('unledgered attempt', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_config_package_hash_independently_matches_current_source(self):
        path = self.session / 'config.json'
        config = load_json(path)
        config['package_sha256'] = 'tampered'
        path.write_text(json.dumps(config))
        self.assertIn('config package identity changed', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_approval_review_id_cannot_change_between_segments(self):
        self.run_session()
        path = self.session / 'execution_authorized.json'
        approval = load_json(path)
        approval['author_review_id'] = 'CHANGED-SYNTHETIC-REVIEW'
        path.write_text(json.dumps(approval))
        self.assertIn('inputs changed on resume', self.run_session()['reason'])
        self.assertEqual(len(self.calls), 72)

    def test_historical_raw_hash_mismatch_refuses_execution(self):
        (self.session / 'historical_c03' / 'synthetic.json').write_text('{}')
        self.assertIn('evidence manifest hash mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_source_snapshot_hash_mismatch_refuses_execution(self):
        (self.session / 'source' / 'synthetic.json').write_text('{}')
        self.assertIn('source snapshot file census/hash mismatch', self.run_session()['reason'])
        self.assertEqual(self.calls, [])

    def test_output_writer_uses_unique_analysis_directory(self):
        destinations = []
        def writer(records, result, destination):
            self.assertTrue((destination / 'analysis.json').exists())
            destinations.append(destination)
            save_json(destination / 'synthetic_summary.json', {'n': len(records)})
        self.deps['output_writer'] = writer
        self.run_session()
        self.run_session()
        self.assertEqual(len(set(destinations)), 2)


if __name__ == '__main__':
    unittest.main()
