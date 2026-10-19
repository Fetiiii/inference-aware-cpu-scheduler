"""Synthetic timing-only tier decisions; no inference or builds."""
from copy import deepcopy
import math
import unittest

from conference.journal_extension.amd_oneshot import protocol
from conference.journal_extension.amd_oneshot.common import digest


class DurationTests(unittest.TestCase):
    def setUp(self):
        self.plan = protocol.load_plan()

    def smoke(self, startup=1):
        return [{**slot, 'status': 'ok', 'startup_elapsed_s': startup,
                 'warmup_elapsed_s': 1, 'request_elapsed_s': 1}
                for slot in protocol.schedule(self.plan, 'technical_smoke')]

    def test_full_static_stop_and_complete_schedules(self):
        for startup, expected in [(1, 'FULL'), (100, 'STATIC_SIGNAL'), (500, 'STOP')]:
            with self.subTest(startup=startup):
                result = protocol.duration_decision(self.plan, self.smoke(startup), 200)
                self.assertEqual(result['selected_tier'], expected)
        self.assertEqual(len(protocol.schedule(self.plan)), 72)
        self.assertEqual(len(protocol.schedule(self.plan, tier='FULL')), 72)
        self.assertEqual(len(protocol.schedule(self.plan, tier='STATIC_SIGNAL')), 48)
        self.assertEqual(protocol.schedule(self.plan, tier='FULL')[:48],
                         protocol.schedule(self.plan, tier='STATIC_SIGNAL'))
        self.assertEqual(self.plan['selected_tier'], 'AUTO')

    def test_scientific_metrics_never_affect_choice_inputs_or_hashes(self):
        smoke = self.smoke(100)
        expected = protocol.duration_decision(self.plan, smoke, 200)
        for record in smoke:
            record.update(ttft_ms=object(), itl_p95_ms=float('nan'), thermal_warning=True,
                          detector={'arbitrary': object()}, decode_tps=-999)
        actual = protocol.duration_decision(self.plan, smoke, 200)
        self.assertEqual(actual, expected)
        self.assertFalse(actual['scientific_metrics_used'])

    def test_cost_uses_larger_backend_total_and_slowest_of_all_four(self):
        smoke = self.smoke()
        all_core = next(record for record in smoke if record['arm'] == 'ALL_CORES12')
        all_core['backend_total_elapsed_s'] = 100
        result = protocol.duration_decision(self.plan, smoke, 0)
        self.assertEqual(result['per_arm_allowance_s']['ALL_CORES12'], 155)
        self.assertGreater(result['estimates']['FULL']['remaining_work_s'], 44 * 33.75)
        self.assertEqual(result['archive_reserve_s'], 600)
        self.assertEqual(result['safety_reserve_s'], 300)

    def test_elapsed_includes_setup_and_prior_build(self):
        smoke = self.smoke()
        self.assertEqual(protocol.duration_decision(self.plan, smoke, 0)['selected_tier'], 'FULL')
        self.assertEqual(protocol.duration_decision(self.plan, smoke, 8000)['selected_tier'], 'STATIC_SIGNAL')
        self.assertEqual(protocol.duration_decision(self.plan, smoke, 10000)['selected_tier'], 'STOP')

    def test_decision_boundary_is_deterministic_and_inclusive(self):
        smoke = self.smoke()
        initial = protocol.duration_decision(self.plan, smoke, 0)
        boundary = self.plan['maximum_session_s'] - initial['estimates']['FULL']['total_forecast_s']
        self.assertEqual(protocol.duration_decision(self.plan, smoke, boundary)['selected_tier'], 'FULL')
        self.assertEqual(protocol.duration_decision(self.plan, smoke, boundary + .001)['selected_tier'], 'STATIC_SIGNAL')

    def test_exactly_four_unique_completed_excluded_technical_slots(self):
        smoke = self.smoke()
        malformed = [smoke[:3], smoke + smoke[:1], [smoke[0]] * 4]
        for key, value in [('role', 'fresh_static_performance'), ('cohort', 'exploration'),
                           ('status', 'failed'), ('arm', 'BIG4'), ('run_id', 'unknown')]:
            changed = deepcopy(smoke)
            # Choose an arm differing from BIG4 even under randomized order.
            index = next(i for i, row in enumerate(changed) if row['arm'] != 'BIG4')
            changed[index][key] = value
            malformed.append(changed)
        for records in malformed:
            with self.subTest(records=len(records)), self.assertRaises(ValueError):
                protocol.duration_decision(self.plan, records, 0)

    def test_nonfinite_boolean_negative_missing_timings_rejected(self):
        for value in (math.nan, math.inf, -1, True, '1', None):
            smoke = self.smoke()
            smoke[0]['startup_elapsed_s'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocol.duration_decision(self.plan, smoke, 0)
        for value in (math.nan, math.inf, -1, True, '1', None):
            with self.subTest(elapsed=value), self.assertRaises(ValueError):
                protocol.duration_decision(self.plan, self.smoke(), value)

    def approval(self):
        return {'authorized': True, 'author_review_id': 'SYNTHETIC',
                'package_sha256': 'source-hash', 'runtime_fallback_authorized': False,
                **protocol.approval_bindings(self.plan)}

    def test_approval_binds_exact_rules_schedules_and_fallback(self):
        approval = self.approval()
        self.assertTrue(protocol.check_approval(self.plan, approval, 'source-hash'))
        bindings = protocol.approval_bindings(self.plan)
        self.assertEqual(bindings['schedule_sha256']['FULL'], digest(protocol.schedule(self.plan, tier='FULL')))
        for field in ('plan_sha256', 'duration_rule_sha256', 'schedule_sha256',
                      'runtime_fallback_policy_sha256', 'package_sha256', 'maximum_session_s'):
            bad = deepcopy(approval)
            bad[field] = 'changed'
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.check_approval(self.plan, bad, 'source-hash')

    def test_no_implicit_fallback_or_manual_tier(self):
        approval = self.approval()
        approval.pop('runtime_fallback_authorized')
        self.assertTrue(protocol.check_approval(self.plan, approval, 'source-hash'))
        approval['runtime_fallback_authorized'] = 'true'
        with self.assertRaises(ValueError):
            protocol.check_approval(self.plan, approval, 'source-hash')
        approval['runtime_fallback_authorized'] = False
        approval['selected_tier'] = 'STATIC_SIGNAL'
        with self.assertRaises(ValueError):
            protocol.check_approval(self.plan, approval, 'source-hash')


if __name__ == '__main__':
    unittest.main()
