import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conference" / "analysis"))
import c04_analyze as analysis  # noqa: E402
from test_c04_runner import trace_events  # noqa: E402


def row(worker, arrival, exit_ns, cpu, migrated=False, q4=True):
    return {
        "run_id": "r", "round": 1, "arm": "PE_ALLOWED_T8_TB16",
        "epoch": 1, "node_index": 7, "worker_index": worker, "active_workers": 2,
        "op": "MUL_MAT", "src0_type": "Q4_K" if q4 else "F32", "is_q4_k": q4,
        "ne0": 256, "ne1": 256, "ne2": 1, "ne3": 1,
        "t_start_ns": 0, "t_arrival_ns": arrival, "t_exit_ns": exit_ns,
        "cpu_start": cpu if not migrated else 0, "cpu_arrival": cpu,
        "cpu_exit": cpu, "migrated_start_to_arrival": int(migrated),
        "cpu_class_at_arrival": "E" if cpu >= 16 else "P",
        "mobility": "migrated" if migrated else ("stable_E" if cpu >= 16 else "stable_P"),
        "compute_duration_us": arrival / 1000, "barrier_wait_us": (exit_ns - arrival) / 1000,
    }


def test_node_grouping_arrival_wait_q4_and_last_e():
    nodes = analysis.group_nodes([row(0, 1000, 5000, 0), row(1, 4000, 5100, 16)])
    assert len(nodes) == 1
    node = nodes[0]
    assert node["arrival_span_us"] == 3.0
    assert node["last_finisher_cpu_class"] == "E"
    assert node["e_minus_p_tail_us"] == 3.0
    assert node["is_q4_k"] is True
    assert node["arrival_offset_vs_barrier_wait_r"] < 0
    assert node["complete_worker_set"] is True
    assert node["e_worker_arrivals"] == 1
    assert node["expected_e_last_finisher_fraction"] == 0.5


def test_migrated_event_exclusion_does_not_fake_complete_node():
    workers = [row(0, 1000, 5000, 0), row(1, 4000, 5100, 16, migrated=True)]
    full = analysis.group_nodes(workers)
    stable = analysis.group_nodes(workers, stable_only=True)
    assert full[0]["complete_worker_set"] is True
    assert stable[0]["complete_worker_set"] is False
    summarized = analysis.arm_node_summary(stable)
    assert summarized["PE_ALLOWED_T8_TB16"]["n_nodes"] == 0


def test_q4_label_keeps_other_mul_mat_separate():
    nodes = analysis.group_nodes([row(0, 1000, 5000, 0, q4=False), row(1, 2000, 5100, 16, q4=False)])
    summary = analysis.arm_node_summary(nodes)
    assert summary["PE_ALLOWED_T8_TB16"]["n_nodes"] == 1
    assert summary["PE_ALLOWED_T8_TB16"]["n_q4_k_nodes"] == 0


def test_worker_metrics_use_raw_timestamps_and_actual_cpus():
    run = {"round": 1, "sequence_index": 1, "arm": "PE_ALLOWED_T8_TB16"}
    raw = trace_events()[0]
    raw.update(cpu_start=0, cpu_arrival=16, t_start_ns=100, t_arrival_ns=1100, t_exit_ns=3100)
    derived = analysis.worker_row(run, raw)
    assert derived["compute_duration_us"] == 1.0
    assert derived["barrier_wait_us"] == 2.0
    assert derived["cpu_class_at_arrival"] == "E"
    assert derived["mobility"] == "migrated"
    assert derived["ne0"] == 256


def test_tied_arrivals_are_not_arbitrarily_e_or_p():
    nodes = analysis.group_nodes([row(0, 1000, 5000, 0), row(1, 1000, 5100, 16)])
    assert nodes[0]["last_finisher_cpu_class"] == "TIE"


def test_zero_counter_and_zero_correlation_have_defined_summary():
    summary = analysis.describe([0, 0])
    assert summary["mean"] == 0
    assert summary["cv_pct"] is None
    assert analysis.pearson([1, 1], [2, 3]) is None


def write_mock_runs(root, invalid_last=False):
    for item in analysis.c04.build_schedule():
        stem = analysis.c04.run_stem(item)
        trace_rel = f"raw/traces/{stem}.jsonl"
        trace_path = root / trace_rel
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        events = trace_events()
        pe = item["arm"] == "PE_ALLOWED_T8_TB16"
        if pe:
            for event in events:
                if event["worker_index"] >= 4:
                    event.update(cpu_start=16 + event["worker_index"] - 4,
                                 cpu_arrival=16 + event["worker_index"] - 4,
                                 cpu_exit=16 + event["worker_index"] - 4)
                    event["t_arrival_ns"] += 100
        invalid = invalid_last and item["global_sequence_index"] == 4
        meta = {"record_type": "meta", "trace_window_graphs": 16,
                "buffer_capacity": 131072, "event_count": len(events),
                "dropped_events": 1 if invalid else 0}
        trace_path.write_text("\n".join(json.dumps(value) for value in [meta, *events]) + "\n", encoding="utf-8")
        run = {**item, "status": "INVALID_TRACE" if invalid else "ok",
               "trace_file": trace_rel, "thermal_throttle_delta": 1 if not pe and item["round"] == 1 else 0,
               "ttft_ms": 9000 if pe else 10000, "itl_p95_ms": 95 if pe else 90,
               "decode_tps": 10 if pe else 11}
        run_path = root / "raw" / "runs" / f"{stem}.json"
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(run), encoding="utf-8")


def test_analysis_round_thermal_and_e_association(tmp_path):
    write_mock_runs(tmp_path)
    summary = analysis.analyze(tmp_path)
    assert summary["run_count"] == 4
    assert summary["questions"]["q2_last_e_fraction"] == 1.0
    assert summary["questions"]["q2_all_arrivals_e_fraction"] == 0.5
    assert summary["questions"]["q1_arrival_span_delta_us"] > 0
    assert summary["questions"]["q5_itl_p95_delta_ms"] == 5
    assert len(summary["thermal_warning_runs"]) == 1
    assert summary["without_thermal_warnings"]["performance"]["P8_ONLY"]["n"] == 1
    assert set(summary["per_round"]) == {"1", "2"}
    for name in ("node_summary.csv", "worker_summary.csv", "summary.json", "summary.md"):
        assert (tmp_path / name).exists()
    markdown = (tmp_path / "summary.md").read_text()
    assert "thermal throttle counters changed" in markdown
    assert "migration away and back" in markdown
    assert "mechanically expected" in markdown
    assert "not independent causal evidence" in markdown
    assert "E-core last-finisher enrichment" in markdown
    entry = summary["node_summary"]["PE_ALLOWED_T8_TB16"]["e_last_finisher"]
    assert entry["e_last_finisher_enrichment"] == 2.0
    stable = summary["stable_event_sensitivity"]["per_arm"]["PE_ALLOWED_T8_TB16"]["e_last_finisher"]
    assert stable == entry


def test_invalid_trace_retained_and_explicitly_excluded(tmp_path):
    write_mock_runs(tmp_path, invalid_last=True)
    summary = analysis.analyze(tmp_path)
    assert len(summary["invalid_runs"]) == 1
    assert summary["instrumented_performance"]["P8_ONLY"]["n"] == 1
    assert summary["node_summary"]["P8_ONLY"]["n_nodes"] == 16
    assert any("invalid trace excluded" in warning for warning in summary["warnings"])
    assert len(list((tmp_path / "raw" / "traces").glob("*.jsonl"))) == 4


def test_analysis_detected_corrupt_trace_cannot_remain_in_performance(tmp_path):
    write_mock_runs(tmp_path)
    raw = sorted((tmp_path / "raw" / "runs").glob("*.json"))[0]
    run = json.loads(raw.read_text())
    trace = tmp_path / run["trace_file"]
    trace.write_text("broken trace", encoding="utf-8")
    summary = analysis.analyze(tmp_path)
    assert len(summary["invalid_runs"]) == 1
    assert summary["instrumented_performance"][run["arm"]]["n"] == 1
    assert json.loads(raw.read_text())["status"] == "ok"


def node_rows(index, cpus, last=0, q4=True, migrated=None, tie=False):
    rows = []
    for worker, cpu in enumerate(cpus):
        arrival = 10000 if tie or worker == last else (worker + 1) * 1000
        item = row(worker, arrival, 11000, cpu, q4=q4, migrated=worker == migrated)
        item.update(node_index=index, active_workers=len(cpus))
        rows.append(item)
    return rows


def enrichment(rows, stable=False, q4=False):
    nodes = analysis.group_nodes(rows, stable_only=stable)
    key = "q4_k_e_last_finisher" if q4 else "e_last_finisher"
    return analysis.arm_node_summary(nodes)["PE_ALLOWED_T8_TB16"][key]


def test_enrichment_adjusts_for_exposure_and_averages_node_probabilities():
    workers = (node_rows(1, [0, 2, 4, 16], last=3)
               + node_rows(2, [0, 16], last=1)
               + node_rows(3, [0, 2, 4, 6], last=3))
    summary = enrichment(workers)
    assert summary["overall_e_worker_arrival_fraction"] == 2 / 10
    assert summary["e_exposed_node_fraction"] == 2 / 3
    assert summary["e_exposed_last_finisher_e_fraction"] == 1
    assert summary["expected_e_last_finisher_fraction"] == 0.375
    assert summary["e_last_finisher_enrichment"] == pytest.approx(1 / 0.375)
    assert summary["observed_minus_expected_e_last_finisher_fraction"] == 0.625
    assert summary["per_node_expected_e_last_finisher_fraction"]["mean"] == 0.25


@pytest.mark.parametrize("empty", [False, True])
def test_no_exposure_or_empty_subset_has_explicit_na(empty):
    summary = enrichment([] if empty else node_rows(1, [0, 2], last=1))
    assert summary["n_e_exposed_nodes"] == 0
    assert summary["e_last_finisher_enrichment"] is None
    assert summary["expected_e_last_finisher_fraction"] is None
    assert summary["e_exposed_last_finisher_e_fraction"] is None
    assert summary["enrichment_status"] == "NA"
    assert "e_last_finisher_enrichment" in summary["na_reasons"]
    assert summary["overall_e_worker_arrival_fraction"] == (None if empty else 0)


def test_all_e_nodes_are_not_enriched_merely_because_e_finishes_last():
    summary = enrichment(node_rows(1, [16, 17], last=1))
    assert summary["overall_e_worker_arrival_fraction"] == 1
    assert summary["e_exposed_node_fraction"] == 1
    assert summary["observed_e_last_finisher_fraction"] == 1
    assert summary["expected_e_last_finisher_fraction"] == 1
    assert summary["e_last_finisher_enrichment"] == 1


def test_zero_observed_e_last_is_a_valid_zero_enrichment_not_na():
    summary = enrichment(node_rows(1, [0, 16], last=0))
    assert summary["expected_e_last_finisher_fraction"] == 0.5
    assert summary["observed_e_last_finisher_fraction"] == 0
    assert summary["e_last_finisher_enrichment"] == 0
    assert summary["enrichment_status"] == "DESCRIPTIVE"


def test_q4_k_enrichment_is_distinct_from_other_mul_mat():
    workers = node_rows(1, [0, 16], last=1) + node_rows(2, [0, 16], last=0, q4=False)
    assert enrichment(workers)["e_last_finisher_enrichment"] == 1
    q4 = enrichment(workers, q4=True)
    assert q4["n_nodes"] == 1
    assert q4["e_last_finisher_enrichment"] == 2


def test_stable_only_enrichment_uses_complete_groups_and_recomputed_baseline():
    workers = node_rows(1, [0, 16], last=1) + node_rows(2, [0, 16], last=0, migrated=1)
    assert enrichment(workers)["e_last_finisher_enrichment"] == 1
    stable = enrichment(workers, stable=True)
    assert stable["n_nodes"] == 1
    assert stable["overall_e_worker_arrival_fraction"] == 0.5
    assert stable["e_last_finisher_enrichment"] == 2
    assert enrichment(workers, stable=True, q4=True) == stable


def test_ties_excluded_from_both_enrichment_sides_with_explicit_counts():
    workers = node_rows(1, [0, 16], last=1) + node_rows(2, [0, 16], tie=True)
    summary = enrichment(workers)
    assert summary["n_e_exposed_nodes"] == 2
    assert summary["e_exposed_last_finisher_e_fraction"] == 0.5
    assert summary["n_e_exposed_tied_last_finisher_nodes"] == 1
    assert summary["n_enrichment_nodes"] == 1
    assert summary["observed_e_last_finisher_fraction"] == 1
    assert summary["expected_e_last_finisher_fraction"] == 0.5
    assert summary["e_last_finisher_enrichment"] == 2
    all_tied = enrichment(node_rows(2, [0, 16], tie=True))
    assert all_tied["n_enrichment_nodes"] == 0
    assert all_tied["e_last_finisher_enrichment"] is None
    assert all_tied["enrichment_status"] == "NA"
