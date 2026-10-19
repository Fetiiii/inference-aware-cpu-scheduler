"""New-study descriptive summaries and predeclared complete-round inference.

No entry point runs an inference workload or touches C01--C06 outputs.
"""
import math
import statistics
import csv
from pathlib import Path
from conference.analysis.stats import paired_analysis, holm_adjust


def _q(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * q
    index = int(position)
    return values[index] + (values[min(index + 1, len(values) - 1)] - values[index]) * (position - index)


def signal_distributions(records, window_ms=200):
    """Request-level distributions, not sample-level independent replications."""
    result = []
    for run in records:
        marker = run.get("t_internal_phase_ns")
        phases = {"pre_marker": [], "post_marker": []}
        if marker is not None:
            for sample in run["samples"]:
                value = sample.get("norm_ctx_per_cpu_s")
                if value is None or not math.isfinite(value) or value < 0:
                    continue
                delta = sample["t_ns"] - marker
                if delta < -window_ms * 1_000_000:
                    phases["pre_marker"].append(value)
                elif delta > window_ms * 1_000_000:
                    phases["post_marker"].append(value)
        row = {"run_id": run["run_id"], "cohort": run["cohort"], "arm": run["arm"],
               "definition": "Operational marker windows, not semantic phase ground truth", "phases": {}}
        for name, values in phases.items():
            row["phases"][name] = {"n_samples": len(values), "q10": _q(values, .1),
                                   "q25": _q(values, .25), "median": _q(values, .5),
                                   "q75": _q(values, .75), "q90": _q(values, .9)}
        a, b = row["phases"].values()
        if a["q25"] is not None and b["q25"] is not None:
            union = max(a["q75"], b["q75"]) - min(a["q25"], b["q25"])
            overlap = max(0, min(a["q75"], b["q75"]) - max(a["q25"], b["q25"]))
            row["iqr_overlap_over_union"] = overlap / union if union > 0 else None
            row["overlap_note"] = "Descriptive IQR overlap; constant union gives NA; not a classifier accuracy"
        result.append(row)
    return result


def analyze(records, plan, gate):
    valid = [r for r in records if r.get("status") == "ok"]
    if len({r["run_id"] for r in valid}) != len(valid):
        raise ValueError("Duplicate completed requests")
    index = {}
    for run in valid:
        key = (run["cohort"], run["round"], run["arm"])
        if key in index:
            raise ValueError("Duplicate cohort/round/arm slot")
        index[key] = run
    comparisons = []
    metrics = plan["statistics"]["primary_metrics"]
    for cohort, contrasts, arms in [
        ("static_confirmatory", plan["statistics"]["static_contrasts"], plan["static_arms"]),
        ("policy_confirmatory", plan["statistics"]["policy_contrasts"], plan["policy_arms"]),
    ]:
        complete = all((cohort, rd, arm) in index for rd in range(1, 7) for arm in arms)
        for arm_a, arm_b in contrasts:
            for metric in metrics:
                item = {"cohort": cohort, "arm_a": arm_a, "arm_b": arm_b, "metric": metric,
                        "status": "COMPLETE" if complete else "NOT_AVAILABLE", "n": 6 if complete else 0,
                        "family": "JE_AMD_PRIMARY_12", "interval_role": "marginal 95%, not simultaneous"}
                if complete:
                    a = [index[(cohort, rd, arm_a)] for rd in range(1, 7)]
                    b = [index[(cohort, rd, arm_b)] for rd in range(1, 7)]
                    item["primary"] = paired_analysis([x[metric] for x in a], [x[metric] for x in b])
                    kept = [(x, y) for x, y in zip(a, b) if not x.get("thermal_warning") and not y.get("thermal_warning")]
                    item["thermal_sensitivity"] = {"n": len(kept), "results":
                        paired_analysis([x[metric] for x, y in kept], [y[metric] for x, y in kept]) if len(kept) >= 2 else None}
                    item["thermal_affected_runs"] = [x["run_id"] for x in a + b if x.get("thermal_warning")]
                else:
                    item["reason"] = f"Policy {gate['status']}" if cohort == "policy_confirmatory" and gate.get("status") in ("NOT_ELIGIBLE", "NOT_PLANNED") else "Six complete rounds not present; no synthetic matching or reduced-n primary test"
                comparisons.append(item)
    if len(comparisons) != 12:
        raise ValueError("Primary family changed")
    adjusted = holm_adjust([x.get("primary", {}).get("p_value", 1.) for x in comparisons])
    for item, p in zip(comparisons, adjusted):
        item["holm_p"] = p if item["status"] == "COMPLETE" else None
        item["multiplicity_bookkeeping_p"] = item.get("primary", {}).get("p_value", 1.)
    groups = {}
    for run in valid:
        groups.setdefault((run["cohort"], run["arm"]), []).append(run)
    summaries = []
    for (cohort, arm), runs in sorted(groups.items()):
        data = {"cohort": cohort, "arm": arm, "n": len(runs), "thermal_warning_ids": [r["run_id"] for r in runs if r.get("thermal_warning")],
                "throttle_unreadable_ids": [r["run_id"] for r in runs if r.get("thermal_throttle_delta") is None], "metrics": {}}
        for metric in ["ttft_ms", "itl_p50_ms", "itl_p95_ms", "itl_p99_ms", "decode_tps"]:
            values = [r[metric] for r in runs if r.get(metric) is not None]
            data["metrics"][metric] = {"mean": statistics.mean(values) if values else None,
                                       "median": statistics.median(values) if values else None}
        data['policy_actions'] = {state: [r['run_id'] for r in runs if r.get('policy_action_status') == state]
                                  for state in ('APPLIED', 'NO_EXTERNAL_TRIGGER', 'NOT_APPLICABLE')}
        summaries.append(data)
    return {"study": plan["study_id"], "synthetic": any(r.get("synthetic") for r in records),
            "valid_requests": len(valid), "arm_summaries": summaries, "primary_comparisons": comparisons,
            "signal_distributions": signal_distributions(valid, plan["calibration"]["window_ms"]),
            "policy_gate": gate,
            "limitations": ["Independent approximately normal round differences assumed; six rounds cannot establish those assumptions.",
                            "No token/sample replication, equivalence or non-inferiority.",
                            "Descriptive signal separation is not semantic precision/recall or proof of scale-only transfer failure.",
                            "This AMD study is not a pooled Intel replication or an isolated architecture interaction experiment."]}


def write_outputs(records, result, destination):
    """Human-review files in a NEW analysis directory, never replace sources."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    fields = ['run_id', 'cohort', 'round', 'sequence_index', 'arm', 'operating_configuration', 'randomized_order_seed',
              'status', 'threads', 'threads_batch', 'phase_source', 'ttft_ms',
              'itl_p50_ms', 'itl_p95_ms', 'itl_p99_ms', 'decode_tps', 'n_tokens',
              'temp_start_c', 'temp_end_c', 'thermal_warning',
              't_request_sent_ns', 't_internal_phase_ns', 't_marker_seen_ns', 't_external_detect_ns']
    with (destination / 'runs.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)
    lines = ['# JE AMD session observations', '',
             'NEW study; technical smoke/training/validation are not confirmatory replications.',
             f'Valid measured requests: {result["valid_requests"]}',
             f'Policy gate: {result["policy_gate"].get("status")}', '',
             'Whole rounds are statistical units; no token/sample pseudoreplication.',
             'Thermally warned valid runs are retained; unreadable counters are UNKNOWN, not zero.', '',
             '| Cohort | Arm | n | TTFT mean ms | ITL p95 mean ms | decode tok/s | Thermal warning runs |',
             '|---|---|---:|---:|---:|---:|---|']
    for group in result['arm_summaries']:
        def value(key):
            x = group['metrics'][key]['mean']
            return 'NA' if x is None else f'{x:.6g}'
        lines.append(f'| {group["cohort"]} | {group["arm"]} | {group["n"]} | {value("ttft_ms")} | {value("itl_p95_ms")} | {value("decode_tps")} | {", ".join(group["thermal_warning_ids"]) or "none"} |')
    lines += ['', '## Policy execution census', '']
    for group in result['arm_summaries']:
        if group['cohort'] == 'policy_confirmatory':
            actions = group['policy_actions']
            lines.append(f'{group["arm"]}: applied={len(actions["APPLIED"])}, no external trigger={len(actions["NO_EXTERNAL_TRIGGER"])}; missed-trigger valid requests remain in performance comparisons.')
    lines += ['', 'Primary family: twelve predeclared slots; Holm across all twelve.',
              'Unavailable policy slots use p=1 for multiplicity bookkeeping only, not an observed test.',
              'Intervals are marginal 95%, not simultaneous. Six round differences cannot validate normality/independence.',
              'No equivalence or non-inferiority claim from a nonsignificant difference.',
              'Criterion offsets are relative to first internally marked unbatched decode computation, not semantic anticipation.',
              'Detailed calibrated/zero-shot validation, decisions and timing remain in validation_gate.json and raw attempts.', '',
              'Selection is frozen before six single-initial-configuration calibration requests; six fresh validation requests do not enter fitting.',
              'Frozen cross-configuration transfer is descriptive in frozen_transfer_report.json; it never triggers retuning or changes eligibility.',
              *result['limitations'], '']
    with (destination / 'summary.md').open('x') as stream:
        stream.write('\n'.join(lines))
