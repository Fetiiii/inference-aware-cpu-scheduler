#!/usr/bin/env python3
"""Descriptive worker-arrival analysis for the bounded TASK-C04B diagnostic."""

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "conference" / "experiments"))

import bench_lib as bl  # noqa: E402
import c04_worker_tail as c04  # noqa: E402


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def describe(values):
    values = [float(value) for value in values if numeric(value)]
    return ({key: round(value, 4) if value is not None else None for key, value in bl.describe(values).items()}
            if values else None)


def pearson(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if numeric(x) and numeric(y)]
    if len(pairs) < 2:
        return None
    xvals, yvals = zip(*pairs)
    xmean, ymean = statistics.fmean(xvals), statistics.fmean(yvals)
    numerator = sum((x - xmean) * (y - ymean) for x, y in pairs)
    denominator = math.sqrt(sum((x - xmean) ** 2 for x in xvals) * sum((y - ymean) ** 2 for y in yvals))
    return round(numerator / denominator, 6) if denominator else None


def worker_row(run, event):
    row = {"run_id": c04.run_stem(run), "round": run["round"], "arm": run["arm"], **event}
    dimensions = event.get("ne", [None, None, None, None])
    for index in range(4):
        row[f"ne{index}"] = dimensions[index] if index < len(dimensions) else None
    row["cpu_class_at_arrival"] = c04.classify_cpu(event["cpu_arrival"])
    row["mobility"] = c04.classify_worker_event(event)
    row["compute_duration_us"] = (event["t_arrival_ns"] - event["t_start_ns"]) / 1000.0
    row["barrier_wait_us"] = (event["t_exit_ns"] - event["t_arrival_ns"]) / 1000.0
    return row


def group_nodes(workers, stable_only=False):
    grouped = defaultdict(list)
    for row in workers:
        if stable_only and row["mobility"] == "migrated":
            continue
        grouped[(row["run_id"], row["epoch"], row["node_index"])].append(row)
    nodes = []
    for rows in grouped.values():
        rows = sorted(rows, key=lambda row: row["worker_index"])
        arrivals = [row["t_arrival_ns"] for row in rows]
        first = min(arrivals)
        p_arrivals = [row["t_arrival_ns"] for row in rows if row["cpu_class_at_arrival"] == "P"]
        e_arrivals = [row["t_arrival_ns"] for row in rows if row["cpu_class_at_arrival"] == "E"]
        last = max(rows, key=lambda row: row["t_arrival_ns"])
        nodes.append({
            "run_id": rows[0]["run_id"], "round": rows[0]["round"], "arm": rows[0]["arm"],
            "epoch": rows[0]["epoch"], "node_index": rows[0]["node_index"],
            "active_workers": rows[0]["active_workers"], "observed_workers": len(rows),
            "e_worker_arrivals": len(e_arrivals),
            "expected_e_last_finisher_fraction": len(e_arrivals) / len(rows),
            "complete_worker_set": (len(rows) == rows[0]["active_workers"] and
                                    {row["worker_index"] for row in rows} == set(range(rows[0]["active_workers"]))),
            "op": rows[0]["op"], "src0_type": rows[0]["src0_type"],
            "is_q4_k": bool(rows[0]["is_q4_k"]),
            "ne0": rows[0]["ne0"], "ne1": rows[0]["ne1"], "ne2": rows[0]["ne2"], "ne3": rows[0]["ne3"],
            "arrival_span_us": (max(arrivals) - first) / 1000.0,
            "max_p_arrival_ns": max(p_arrivals) if p_arrivals else None,
            "max_e_arrival_ns": max(e_arrivals) if e_arrivals else None,
            "e_minus_p_tail_us": ((max(e_arrivals) - max(p_arrivals)) / 1000.0) if p_arrivals and e_arrivals else None,
            "last_finisher_cpu_class": (last["cpu_class_at_arrival"] if
                                         sum(row["t_arrival_ns"] == last["t_arrival_ns"] for row in rows) == 1 else "TIE"),
            "src0_ne": rows[0].get("src0_ne"), "src1_ne": rows[0].get("src1_ne"),
            "arrival_offset_vs_barrier_wait_r": pearson(
                [(row["t_arrival_ns"] - first) / 1000.0 for row in rows],
                [row["barrier_wait_us"] for row in rows]),
            "mean_barrier_wait_us": statistics.fmean(row["barrier_wait_us"] for row in rows),
            "migrated_worker_events": sum(row["mobility"] == "migrated" for row in rows),
        })
    return sorted(nodes, key=lambda row: (row["run_id"], row["epoch"], row["node_index"]))


def load(root):
    runs, workers, warnings = [], [], []
    for path in sorted((root / "raw" / "runs").glob("round_*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        if run.get("arm") not in c04.ARMS:
            warnings.append(f"ignored unknown arm in {path.name}")
            continue
        if run.get("status") != "ok":
            warnings.append(f"{path.name} status={run.get('status')}")
        runs.append(run)
        try:
            meta, events = c04.read_trace(root / run["trace_file"])
            errors = c04.trace_validation_errors(meta, events)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors, events = [str(exc)], []
        if errors:
            warnings.append(f"{path.name}: {'; '.join(errors)}")
            # Derived validity only: preserve the original raw record on disk.
            run["captured_status"] = run.get("status")
            run["status"] = "INVALID_TRACE_AT_ANALYSIS"
        if run.get("status") == "ok" and not errors:
            workers.extend(worker_row(run, event) for event in events)
        else:
            warnings.append(f"{path.name}: invalid trace excluded from derived worker/node comparisons; raw data retained")
    return runs, workers, warnings


def e_last_finisher_summary(nodes):
    """Participation-adjusted descriptive baseline, without causal inference.

    Every input node is a complete group. Enrichment uses the same E-exposed,
    unique-last node set for observed and expected fractions; ties are counted
    explicitly and excluded from both sides of that comparison.
    """
    na_reasons = {}

    def fraction(numerator, denominator, key):
        if denominator is None or denominator == 0:
            na_reasons[key] = "NA: denominator is zero or unavailable"
            return None
        return numerator / denominator

    exposed = [node for node in nodes if node["e_worker_arrivals"] > 0]
    comparable = [node for node in exposed if node["last_finisher_cpu_class"] != "TIE"]
    e_last = sum(node["last_finisher_cpu_class"] == "E" for node in exposed)
    observed = fraction(e_last, len(comparable), "observed_e_last_finisher_fraction")
    expected = (statistics.fmean(node["expected_e_last_finisher_fraction"] for node in comparable)
                if comparable else None)
    if expected is None:
        na_reasons["expected_e_last_finisher_fraction"] = "NA: no E-exposed nodes with a unique last finisher"
    enrichment = fraction(observed, expected, "e_last_finisher_enrichment")
    output = {
        "n_nodes": len(nodes),
        "n_worker_arrivals": sum(node["observed_workers"] for node in nodes),
        "n_e_worker_arrivals": sum(node["e_worker_arrivals"] for node in nodes),
        "n_e_exposed_nodes": len(exposed),
        "n_e_last_finisher_nodes": e_last,
        "n_tied_last_finisher_nodes": sum(node["last_finisher_cpu_class"] == "TIE" for node in nodes),
        "n_e_exposed_tied_last_finisher_nodes": len(exposed) - len(comparable),
        "n_enrichment_nodes": len(comparable),
        "overall_e_worker_arrival_fraction": fraction(
            sum(node["e_worker_arrivals"] for node in nodes),
            sum(node["observed_workers"] for node in nodes),
            "overall_e_worker_arrival_fraction"),
        "e_exposed_node_fraction": fraction(len(exposed), len(nodes), "e_exposed_node_fraction"),
        "e_exposed_last_finisher_e_fraction": fraction(e_last, len(exposed), "e_exposed_last_finisher_e_fraction"),
        "per_node_expected_e_last_finisher_fraction": describe(node["expected_e_last_finisher_fraction"] for node in nodes),
        "observed_e_last_finisher_fraction": observed,
        "expected_e_last_finisher_fraction": expected,
        "e_last_finisher_enrichment": enrichment,
        "observed_minus_expected_e_last_finisher_fraction": observed - expected if observed is not None and expected is not None else None,
        "enrichment_status": "NA" if enrichment is None else "DESCRIPTIVE",
        "na_reasons": na_reasons,
        "method": "Expected probability per node = E arrivals / all worker arrivals. Enrichment = observed / mean expected on the same E-exposed, unique-last nodes; ties excluded from both. No significance or causal inference.",
    }
    return output


def arm_node_summary(nodes):
    output = {}
    for arm in c04.ARMS:
        selected = [node for node in nodes if node["arm"] == arm and node["complete_worker_set"]]
        q4 = [node for node in selected if node["is_q4_k"]]
        output[arm] = {
            "n_nodes": len(selected), "n_q4_k_nodes": len(q4),
            "e_last_finisher": e_last_finisher_summary(selected),
            "q4_k_e_last_finisher": e_last_finisher_summary(q4),
            "arrival_span_us": describe(node["arrival_span_us"] for node in selected),
            "q4_k_arrival_span_us": describe(node["arrival_span_us"] for node in q4),
            "barrier_wait_us": describe(node["mean_barrier_wait_us"] for node in selected),
            "q4_k_barrier_wait_us": describe(node["mean_barrier_wait_us"] for node in q4),
            "last_finisher_e_fraction": (sum(node["last_finisher_cpu_class"] == "E" for node in selected) / len(selected)) if selected else None,
            "q4_k_last_finisher_e_fraction": (sum(node["last_finisher_cpu_class"] == "E" for node in q4) / len(q4)) if q4 else None,
            "e_minus_p_tail_us": describe(node["e_minus_p_tail_us"] for node in selected),
            "q4_k_e_minus_p_tail_us": describe(node["e_minus_p_tail_us"] for node in q4),
            "arrival_vs_wait_r": describe(node["arrival_offset_vs_barrier_wait_r"] for node in selected),
            "q4_k_arrival_vs_wait_r": describe(node["arrival_offset_vs_barrier_wait_r"] for node in q4),
        }
    return output


def arm_worker_summary(workers):
    output = {}
    for arm in c04.ARMS:
        selected = [row for row in workers if row["arm"] == arm]
        counts = Counter(row["mobility"] for row in selected)
        arrival_classes = Counter(row["cpu_class_at_arrival"] for row in selected)
        output[arm] = {
            "n_worker_events": len(selected), "mobility_counts": dict(sorted(counts.items())),
            "mobility_fractions": ({key: value / len(selected) for key, value in sorted(counts.items())} if selected else {}),
            "arrival_cpu_class_counts": dict(sorted(arrival_classes.items())),
            "arrival_e_fraction": (arrival_classes["E"] / len(selected) if selected else None),
            "compute_duration_us": describe(row["compute_duration_us"] for row in selected),
            "barrier_wait_us": describe(row["barrier_wait_us"] for row in selected),
        }
    return output


def performance(runs):
    output = {}
    for arm in c04.ARMS:
        selected = [run for run in runs if run["arm"] == arm and run.get("status") == "ok"]
        output[arm] = {"n": len(selected)}
        for metric in ("ttft_ms", "itl_p95_ms", "decode_tps"):
            output[arm][metric] = describe(run.get(metric) for run in selected)
        for metric in ("temp_start_c", "temp_end_c", "thermal_throttle_delta"):
            output[arm][metric] = describe(run.get(metric) for run in selected)
    return output


def delta(summary, metric):
    p = summary["P8_ONLY"].get(metric)
    pe = summary["PE_ALLOWED_T8_TB16"].get(metric)
    return round(pe["mean"] - p["mean"], 4) if p and pe else None


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def markdown(summary):
    ns = summary["node_summary"]
    lines = [
        "# TASK-C04B worker-tail observations", "",
        "This report is descriptive mechanism instrumentation. It does not infer causality, and instrumented TTFT/ITL are sanity metrics rather than primary paper performance data.", "",
        "## Six bounded questions", "",
        f"1. PE_ALLOWED minus P8_ONLY mean arrival span: **{summary['questions']['q1_arrival_span_delta_us']} us**.",
        f"2. PE_ALLOWED last-arriving-on-E fraction: **{summary['questions']['q2_last_e_fraction']}**; all worker arrivals on E: **{summary['questions']['q2_all_arrivals_e_fraction']}**.",
        f"3. PE_ALLOWED arrival-offset/barrier-wait correlation: **{summary['questions']['q3_arrival_vs_wait_r']}**. Negative correlation is mechanically expected at a synchronization barrier: earlier arrivals have longer to wait for the same release. This is not independent causal evidence.",
        f"4. Q4_K PE-minus-P mean arrival-span difference: **{summary['questions']['q4_q4_k_arrival_span_delta_us']} us**.",
        f"5. Instrumented PE-minus-P decode direction: ITL p95 **{summary['questions']['q5_itl_p95_delta_ms']} ms**, throughput **{summary['questions']['q5_decode_tps_delta']} tok/s**.",
        f"6. Excluding migrated events, complete-node PE-minus-P arrival span: **{summary['questions']['q6_stable_only_arrival_span_delta_us']} us**.", "",
        "## Warnings", "",
    ]
    lines.extend(f"- {warning}" for warning in summary["warnings"])
    if not summary["warnings"]:
        lines.append("- None recorded.")
    lines += ["", "## E-core last-finisher enrichment", "",
              "All fractions use complete node groups. The table distinguishes all worker arrivals, E-exposed nodes, and the matched E-exposed/unique-last subset used for enrichment. Per-node expected probabilities and participation counts are retained in node_summary.csv. NA denotes a zero/unavailable denominator; summary.json records the reason.", "",
              "Tied-last nodes are included in the exposure fraction but are not counted as a uniquely E last finisher. They are excluded from both observed and expected enrichment denominators and counted separately.", ""]
    for title, summaries in (("All valid complete nodes", ns),
                             ("Stable workers only; complete groups", summary["stable_event_sensitivity"]["per_arm"])):
        lines += [f"### {title}", "",
                  "| Arm / nodes | n | E arrival fraction | E-exposed fraction | E-last / E-exposed | Matched n | Observed E-last | Expected E-last | Enrichment | E-exposed ties |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for arm in c04.ARMS:
            for subset, key in (("all MUL_MAT", "e_last_finisher"), ("Q4_K", "q4_k_e_last_finisher")):
                entry = summaries[arm][key]
                values = [entry[field] for field in (
                    "n_nodes", "overall_e_worker_arrival_fraction", "e_exposed_node_fraction",
                    "e_exposed_last_finisher_e_fraction", "n_enrichment_nodes",
                    "observed_e_last_finisher_fraction", "expected_e_last_finisher_fraction",
                    "e_last_finisher_enrichment", "n_e_exposed_tied_last_finisher_nodes")]
                formatted = ["NA" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value) for value in values]
                lines.append(f"| {arm} / {subset} | " + " | ".join(formatted) + " |")
        lines.append("")
    lines += ["", "## Instrumented sanity metrics and thermal state", "", "```json",
              json.dumps(summary["instrumented_performance"], indent=2), "```", "",
              "## Per-arm node summary", "", "```json", json.dumps(ns, indent=2), "```", "",
              "## Round-level observations", "", "```json", json.dumps(summary["per_round"], indent=2), "```", "",
              "## Thermal sensitivity", "",
              "Warned runs remain in the primary descriptive summary. The following excludes them for a sensitivity comparison; scientific interpretation must report whether its direction changes.",
              "", "```json", json.dumps(summary["without_thermal_warnings"], indent=2), "```", "",
              "## Observation limits", "",
              "CPU classes use observed CPU IDs. Stable means identical start/arrival CPU samples; migration away and back between samples is not observable. Barrier wait includes the barrier implementation and scheduler delay, and the single shared-buffer reservation before node start can perturb worker timing.",
              "", "The trace is dumped synchronously after graph 16; that interval can inflate an instrumented ITL. Q4_K_M is mixed-tensor: Q4_K labels do not describe all decode work. Vector-width ablation remains closed.", ""]
    return "\n".join(lines)


def analyze(root):
    c04.protect_historical_outputs(root)
    root = Path(root)
    if not (root / "raw" / "runs").is_dir():
        raise ValueError("C04 raw run directory is missing; no derived output written")
    runs, workers, warnings = load(root)
    nodes, stable_nodes = group_nodes(workers), group_nodes(workers, stable_only=True)
    node_summary, stable_summary = arm_node_summary(nodes), arm_node_summary(stable_nodes)
    perf = performance(runs)
    thermal = [c04.run_stem(run) for run in runs if (run.get("thermal_throttle_delta") or 0) > 0]
    if thermal:
        warnings.append("thermal throttle counters changed in: " + ", ".join(thermal))
    unreadable_thermal = [c04.run_stem(run) for run in runs if run.get("thermal_throttle_delta") is None]
    if unreadable_thermal:
        warnings.append("thermal throttle counters unavailable in: " + ", ".join(unreadable_thermal))
    if len(runs) != 4:
        warnings.append(f"expected 4 runs; observed {len(runs)}")
    worker_summary = arm_worker_summary(workers)
    per_round = {}
    for round_number in (1, 2):
        round_runs = [run for run in runs if run["round"] == round_number]
        round_nodes = [node for node in nodes if node["round"] == round_number]
        round_workers = [row for row in workers if row["round"] == round_number]
        per_round[str(round_number)] = {"nodes": arm_node_summary(round_nodes),
                                      "workers": arm_worker_summary(round_workers),
                                      "performance": performance(round_runs)}
        if Counter(run["arm"] for run in round_runs) != Counter(c04.ARMS):
            warnings.append(f"round {round_number} does not contain both arms exactly once")
    unwarned_runs = [run for run in runs if c04.run_stem(run) not in thermal]
    unwarned_nodes = [node for node in nodes if node["run_id"] not in thermal]
    unwarned_summary = arm_node_summary(unwarned_nodes)
    summary = {
        "schema_version": 1, "task": c04.TASK, "run_count": len(runs),
        "node_summary": node_summary, "worker_summary": worker_summary,
        "per_round": per_round,
        "without_thermal_warnings": {"nodes": unwarned_summary,
                                     "arrival_span_delta_us": delta(unwarned_summary, "arrival_span_us"),
                                     "performance": performance(unwarned_runs)},
        "stable_event_sensitivity": {"per_arm": stable_summary, "arrival_span_delta_us": delta(stable_summary, "arrival_span_us"), "note": "Only complete worker sets after migrated-event removal contribute."},
        "instrumented_performance": perf,
        "questions": {
            "q1_arrival_span_delta_us": delta(node_summary, "arrival_span_us"),
            "q2_last_e_fraction": node_summary["PE_ALLOWED_T8_TB16"]["last_finisher_e_fraction"],
            "q2_all_arrivals_e_fraction": worker_summary["PE_ALLOWED_T8_TB16"]["arrival_e_fraction"],
            "q2_e_exposed_node_fraction": node_summary["PE_ALLOWED_T8_TB16"]["e_last_finisher"]["e_exposed_node_fraction"],
            "q2_e_exposed_last_finisher_e_fraction": node_summary["PE_ALLOWED_T8_TB16"]["e_last_finisher"]["e_exposed_last_finisher_e_fraction"],
            "q2_expected_e_last_finisher_fraction": node_summary["PE_ALLOWED_T8_TB16"]["e_last_finisher"]["expected_e_last_finisher_fraction"],
            "q2_e_last_finisher_enrichment": node_summary["PE_ALLOWED_T8_TB16"]["e_last_finisher"]["e_last_finisher_enrichment"],
            "q3_arrival_vs_wait_r": node_summary["PE_ALLOWED_T8_TB16"]["arrival_vs_wait_r"],
            "q4_q4_k_arrival_span_delta_us": delta(node_summary, "q4_k_arrival_span_us"),
            "q4_q4_k_e_last_finisher_enrichment": node_summary["PE_ALLOWED_T8_TB16"]["q4_k_e_last_finisher"]["e_last_finisher_enrichment"],
            "q5_itl_p95_delta_ms": delta(perf, "itl_p95_ms"),
            "q5_decode_tps_delta": delta(perf, "decode_tps"),
            "q6_stable_only_arrival_span_delta_us": delta(stable_summary, "arrival_span_us"),
            "q6_stable_only_e_last_finisher_enrichment": stable_summary["PE_ALLOWED_T8_TB16"]["e_last_finisher"]["e_last_finisher_enrichment"],
        },
        "thermal_warning_runs": thermal, "warnings": warnings,
        "invalid_runs": [c04.run_stem(run) for run in runs if run.get("status") != "ok"],
        "interpretation": "Checkpoint review required; no outcome or causal mechanism is automatically declared.",
    }
    write_csv(root / "worker_summary.csv", workers)
    write_csv(root / "node_summary.csv", nodes)
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (root / "summary.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze TASK-C04B worker-tail traces descriptively")
    parser.add_argument("--input", default=str(ROOT / "results" / "conference_c04" / "worker_tail"))
    args = parser.parse_args(argv)
    summary = analyze(args.input)
    print(json.dumps(summary["questions"], indent=2))
    print(f"Wrote {args.input}/summary.json and summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
