"""New-study synthetic fixtures only: no inference or real evidence writes."""
import copy
import unittest

from conference.journal_extension.amd_oneshot.calibration import (
    DetectorState, detect_decisions, evaluate, fit, gate_policy, zero_shot_model,
    select_opportunity, transfer_report,
)


def run(run_id="train", cohort="calibration", **extra):
    values = [100.] * 25 + [4000.] * 25
    return dict(run_id=run_id, cohort=cohort, arm="BIG4", round=1,
                ttft_ms=100., itl_p95_ms=100., decode_tps=10.,
                t_request_sent_ns=0, t_internal_phase_ns=500_000_000,
                samples=[dict(t_ns=i*20_000_000, norm_ctx_per_cpu_s=v)
                         for i, v in enumerate(values)], **extra)


def training():
    return [run(f't{i}') for i in range(6)]


def validation():
    return [run(f'v{i}', 'validation') for i in range(6)]


def exploration():
    result = []
    for rd in (1, 2):
        for arm, ttft, itl in (("BIG4", 80, 100), ("COMPACT4", 100, 80),
                               ("ALL_ALLOWED4", 110, 110), ('ALL_CORES12', 60, 120)):
            r = run(f"e{rd}-{arm}", 'exploration')
            r.update(round=rd, arm=arm, ttft_ms=ttft, itl_p95_ms=itl)
            result.append(r)
    return result


class CalibrationTests(unittest.TestCase):
    def test_opposite_polarity_configurations_cannot_be_pooled(self):
        train = training()
        train[3]['operating_configuration'] = 'COMPACT4'
        for sample in train[3]['samples']:
            sample['norm_ctx_per_cpu_s'] = 4100 - sample['norm_ctx_per_cpu_s']
        with self.assertRaisesRegex(ValueError, 'pooled'):
            fit(train, {})
        model = fit(training(), {})
        vals = validation()
        for item in vals:
            item['operating_configuration'] = 'COMPACT4'
        with self.assertRaisesRegex(ValueError, 'differs'):
            evaluate(vals, model, {})
        self.assertEqual(evaluate(vals, zero_shot_model(), {})['requests'], 6)

    def test_selection_requires_full_exploration_and_no_opportunity_reference(self):
        explore = exploration()
        with self.assertRaisesRegex(ValueError, 'complete'):
            select_opportunity(explore[:-1], {})
        before = select_opportunity(explore, {})
        self.assertEqual(before, select_opportunity(list(reversed(explore)), {}))
        for item in explore:
            item['ttft_ms'] = item['itl_p95_ms'] = 100
        selection = select_opportunity(explore, {})
        self.assertEqual(selection['initial_mask'], 'ALL_ALLOWED4')
        self.assertIsNone(selection['policy'])

    def test_exploration_train_overlap_is_rejected(self):
        explore = exploration(); train = training()
        train[0]['run_id'] = explore[0]['run_id']
        model = fit(train, {})
        report = evaluate(validation(), model, {})
        with self.assertRaisesRegex(ValueError, 'overlap'):
            gate_policy(explore, report, model, {})

    def test_transfer_is_frozen_and_separate_per_configuration(self):
        model = fit(training(), {}); frozen = copy.deepcopy(model)
        records = []
        for arm in ('BIG4', 'COMPACT4', 'ALL_ALLOWED4', 'ALL_CORES12'):
            for i in range(6):
                item = run(f's-{arm}-{i}', 'static_confirmatory')
                item['operating_configuration'] = arm
                records.append(item)
        report = transfer_report(records, model, {})
        self.assertEqual(model, frozen)
        self.assertEqual(report['reports']['BIG4']['domain'], 'initial_configuration_matched')
        self.assertEqual(report['reports']['COMPACT4']['domain'], 'other_four_worker_placement')
        self.assertEqual(report['reports']['ALL_CORES12']['domain'], 'worker_count_and_eligibility_confound')
        records[0]['cohort'] = 'exploration'
        with self.assertRaisesRegex(ValueError, 'cohort'):
            transfer_report(records, model, {})

    def test_train_only_deterministic_and_input_preserved(self):
        train = training()
        original = copy.deepcopy(train)
        model = fit(train, {})
        self.assertEqual(model, fit(train, {}))
        self.assertEqual(model, fit(list(reversed(train)), {}))
        self.assertEqual(model["mode"], "recalibration")
        self.assertEqual(train, original)
        self.assertNotEqual(model["mode"], zero_shot_model()["mode"])

    def test_cohort_overlap_and_freeze(self):
        with self.assertRaises(ValueError):
            fit([run(cohort="validation")], {})
        model = fit(training(), {})
        with self.assertRaises(ValueError):
            evaluate([run(cohort="validation")], model, {})
        with self.assertRaises(ValueError):
            evaluate([run("v", "validation")], model, {"window_ms":100})
        mutated = dict(model, hi=model["hi"]+1)
        with self.assertRaises(ValueError):
            evaluate([run("v", "validation")], mutated, {})

    def test_marker_blind_timestamp_and_invalid_persistence(self):
        model = zero_shot_model()
        samples = [dict(t_ns=i*20_000_000, norm_ctx_per_cpu_s=v)
                   for i, v in enumerate([4000., None, 4000., 4000., 100., 100.])]
        self.assertEqual(detect_decisions(samples, model),
                         [dict(t_ns=60_000_000, state="decode"),
                          dict(t_ns=100_000_000, state="prefill")])
        samples[1]["norm_ctx_per_cpu_s"] = float("nan")
        self.assertEqual(detect_decisions(samples, model)[0]["t_ns"], 60_000_000)

    def test_eligibility_strict_boundary_and_spurious(self):
        model = zero_shot_model()
        v = run("v", "validation")
        vals = validation(); vals[0] = v
        report = evaluate(vals, model, {})
        self.assertEqual(report["matched_requests"], 6)
        self.assertEqual(report["spurious_events"], 0)
        self.assertTrue(report["precision_undefined"])
        # A single early decode cannot be replaced by a later correct crossing.
        for i in (1, 2):
            v["samples"][i]["norm_ctx_per_cpu_s"] = 4000.
        report = evaluate(vals, model, {})
        self.assertEqual(report["matched_requests"], 5)
        self.assertEqual(report["spurious_events"], 3)
        v["t_internal_phase_ns"] = None
        report = evaluate(vals, model, {})
        self.assertEqual(report["coverage"], 1.)
        self.assertEqual(report["ineligible_requests"], 1)

    def test_gate_freezes_budget_endpoints_and_comparator(self):
        train = training()
        model = fit(train, {})
        report = evaluate(validation(), model, {})
        gate = gate_policy(exploration(), report, model, {})
        self.assertEqual(gate["status"], "ELIGIBLE")
        self.assertEqual(gate["policy"], dict(initial_mask="BIG4", final_mask="COMPACT4",
                                               prefill_workers=4, decode_workers=4))
        self.assertEqual(gate["static_comparator"], "BIG4")
        report["thermal_warning"] = True
        failed = gate_policy(exploration(), report, model, {})
        self.assertEqual(failed["status"], "NOT_ELIGIBLE")
        self.assertIsNone(failed["policy"])
        self.assertFalse(failed["retuning_allowed"])

    def test_thermal_training_and_round_disagreement_fail_closed(self):
        train = training()
        train[0]["thermal_warning"] = True
        explore = exploration(); explore[0]['ttft_ms'] = 110
        model = fit(train, {})
        report = evaluate(validation(), model, {})
        gate = gate_policy(explore, report, model, {})
        self.assertIn("THERMAL_WARNING", gate["reasons"])
        self.assertIn("ROUND_DIRECTION_DISAGREEMENT", gate["reasons"])

    def test_validation_failure_never_selects_alternative(self):
        train = training()
        model = fit(train, {})
        vals = validation(); vals[0]['t_internal_phase_ns'] = None
        report = evaluate(vals, model, {})
        gate = gate_policy(exploration(), report, model, {})
        self.assertEqual(gate["status"], "NOT_ELIGIBLE")
        self.assertIn("INSUFFICIENT_ELIGIBLE_VALIDATION", gate["reasons"])
        self.assertIsNone(gate["policy"])

    def test_strict_eligibility_edges_and_no_training_eligible(self):
        model = zero_shot_model()
        v = run("v", "validation")
        # 300ms is exactly marker-200ms and cannot be counted as before.
        v["samples"] = [s for s in v["samples"] if s["t_ns"] >= 260_000_000]
        vals = validation(); vals[0] = v
        self.assertEqual(evaluate(vals, model, {})["eligible_requests"], 5)
        t = run()
        t["t_internal_phase_ns"] = None
        with self.assertRaises(ValueError):
            fit([t], {})

    def test_invalid_values_do_not_enter_log_training(self):
        train = training()
        train[0]["samples"][0]["norm_ctx_per_cpu_s"] = float("nan")
        train[0]["samples"][1]["norm_ctx_per_cpu_s"] = -10
        model = fit(train, {"transforms":["log1p"]})
        self.assertTrue(model["hi"] > model["lo"] >= 0)
        self.assertEqual(model["transform"], "log1p")

    def test_jitter_and_live_offline_identical(self):
        model = dict(zero_shot_model(), k=3)
        samples = [dict(t_ns=t, norm_ctx_per_cpu_s=4000.)
                   for t in (0, 20_200_000, 41_700_000)]
        detector = DetectorState(model)
        live = [event for sample in samples if (event := detector.feed(sample)) is not None]
        self.assertEqual(live, [dict(t_ns=41_700_000, state="decode")])
        self.assertEqual(live, detect_decisions(samples, model))

    def test_excessive_or_nonpositive_gaps_reset(self):
        model = zero_shot_model()
        for times in ((0, 50_000_001, 70_200_001), (0, 0, 20_200_000, 41_700_000),
                      (20_000_000, 0, 40_000_000, 60_000_000)):
            samples = [dict(t_ns=t, norm_ctx_per_cpu_s=4000.) for t in times]
            self.assertEqual(detect_decisions(samples, model),
                             [dict(t_ns=times[-1], state="decode")])

    def test_equal_thresholds_are_not_crossings(self):
        model = zero_shot_model()
        values = [3000., 3000., 3001., 3001., 2100., 2100., 2099., 2099.]
        samples = [dict(t_ns=i*20_000_000, norm_ctx_per_cpu_s=v)
                   for i, v in enumerate(values)]
        self.assertEqual(detect_decisions(samples, model),
                         [dict(t_ns=60_000_000, state="decode"),
                          dict(t_ns=140_000_000, state="prefill")])
        falling = dict(model, polarity="falling")
        values = [2100., 2100., 2099., 2099., 3000., 3000., 3001., 3001.]
        for sample, value in zip(samples, values):
            sample["norm_ctx_per_cpu_s"] = value
        self.assertEqual(detect_decisions(samples, falling),
                         [dict(t_ns=60_000_000, state="decode"),
                          dict(t_ns=140_000_000, state="prefill")])

    def test_live_model_integrity_and_backend_throttle_warning(self):
        train = training()
        model = fit(train, {})
        with self.assertRaises(ValueError):
            DetectorState(dict(model, lo=model["lo"]-1))
        with self.assertRaises(ValueError):
            DetectorState(dict(model, protocol_hash="mutated"))
        vals = validation()
        vals[0]["throttle_warning"] = True
        report = evaluate(vals, model, {})
        self.assertIn("THERMAL_WARNING", gate_policy(exploration(), report, model, {})["reasons"])


if __name__ == "__main__":
    unittest.main()
