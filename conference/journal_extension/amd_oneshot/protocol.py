"""Frozen complete-round schedules and approval/budget validation."""
from pathlib import Path
import math
import random
from .common import digest, load_json

PLAN_PATH = Path(__file__).with_name("RUN_PLAN.json")


def load_plan(path=PLAN_PATH):
    plan = load_json(path)
    if plan["static_arms"] != ["BIG4", "COMPACT4", "ALL_ALLOWED4", "ALL_CORES12"]:
        raise ValueError("Unexpected static arm set")
    if plan["policy_arms"] != ["STOCK", "STATIC", "EXTERNAL", "ORACLE"]:
        raise ValueError("Unexpected policy arm set")
    if plan["policy_worker_budget"] != {"threads": 4, "threads_batch": 4}:
        raise ValueError("Policy worker budget must remain 4/4")
    expected = {"technical_smoke": 1, "exploration": 2, "calibration": 6, "validation": 6,
                "static_confirmatory": 6, "policy_confirmatory": 6}
    if {x["name"]: x["rounds"] for x in plan["cohorts"]} != expected:
        raise ValueError("Unapproved cohort/sample-count amendment")
    if plan["calibration"]["interval_ms"] != 20:
        raise ValueError("Frozen observation interval must be 20 ms")
    if plan.get('protocol_version') != 'JE-AMD-01-v3' or plan.get('selected_tier') != 'AUTO':
        raise ValueError('Unapproved protocol revision or execution tier')
    rule = plan.get('duration_rule', {})
    if (rule.get('timing_fields') != ['startup_elapsed_s', 'warmup_elapsed_s', 'request_elapsed_s', 'backend_total_elapsed_s']
            or rule.get('timing_overhead_fraction') != 0.25 or rule.get('safety_reserve_s') != 300
            or rule.get('archive_reserve_s') != plan['archive_reserve_s']
            or plan['archive_reserve_s'] != 600 or rule.get('priority') != ['FULL', 'STATIC_SIGNAL', 'STOP']):
        raise ValueError('Unapproved duration rule')
    fallback = plan.get('runtime_fallback_policy', {})
    from .runtime_fallback import POLICY
    if (fallback.get('maximum_build_s') != 900 or fallback.get('parallel_jobs') != 4
            or fallback != POLICY):
        raise ValueError('Unapproved runtime fallback policy')
    if plan.get('selected_arms') != ['INITIAL_SELECTED']:
        raise ValueError('Calibration/validation require one frozen selected initial configuration')
    if plan['zero_shot'] != {'interval_ms': 20, 'max_gap_ms': 50, 'hi': 3000,
                            'lo': 2100, 'k': 2, 'transform': 'identity', 'polarity': 'rising'}:
        raise ValueError('Frozen Intel zero-shot parameters changed')
    if plan["cooldown_s"] < 30 or plan["initial_cooldown_s"] < 30:
        raise ValueError("Cooldown below frozen minimum")
    return plan


def schedule(plan, cohort=None, tier=None):
    tier = tier or plan['selected_tier']
    if tier == 'AUTO':
        tier = 'FULL'
    if tier not in ('FULL', 'STATIC_SIGNAL'):
        raise ValueError('Unknown duration-selected tier')
    result = []
    for stage in plan["cohorts"]:
        if stage['name'] == 'policy_confirmatory' and tier == 'STATIC_SIGNAL':
            continue
        if cohort is not None and stage["name"] != cohort:
            continue
        arms = plan[stage["arms"] + "_arms"]
        for round_number in range(1, stage["rounds"] + 1):
            seed = plan["order_seed"] + stage["seed_offset"] + round_number
            order = list(arms)
            random.Random(seed).shuffle(order)
            for seq, arm in enumerate(order, 1):
                result.append({"run_id": f"je01_{stage['name']}_r{round_number:02d}_{arm.lower()}",
                               "cohort": stage["name"], "role": stage["role"], "round": round_number,
                               "sequence_index": seq, "arm": arm, "randomized_order_seed": seed})
    return result


def static_specs(topology):
    masks = topology.get("masks", topology)
    big = list(masks["BIG4"])
    compact = list(masks["COMPACT4"])
    all_cpus = list(masks["ALL_ALLOWED4"])
    if len(set(big)) != 4 or len(big) != 4 or len(set(compact)) != 4 or len(compact) != 4 or len(set(all_cpus)) != 12 or len(all_cpus) != 12:
        raise ValueError("Expected four/four/twelve physical representatives")
    if masks['ALL_CORES12'] != all_cpus:
        raise ValueError('ALL_CORES12 must use identical eligibility to ALL_ALLOWED4')
    if set(big) & set(compact) or not set(big + compact) <= set(all_cpus):
        raise ValueError("Invalid static mask relationship")
    return {arm: {"cpus": list(masks[arm]), "target_cpus": None,
                  "threads": 12 if arm == "ALL_CORES12" else 4,
                  "threads_batch": 12 if arm == "ALL_CORES12" else 4,
                  "phase_source": "none"}
            for arm in ["BIG4", "COMPACT4", "ALL_ALLOWED4", "ALL_CORES12"]}


def policy_specs(topology, gate):
    if gate["status"] != "ELIGIBLE":
        raise ValueError("Policy experiment is NOT_ELIGIBLE")
    static = static_specs(topology)
    policy = gate["policy"]
    initial = static[policy["initial_mask"]]["cpus"]
    final = static[policy["final_mask"]]["cpus"]
    if initial == final or policy["decode_workers"] != 4 or policy["prefill_workers"] != 4:
        raise ValueError("No eligible fixed-budget placement transition")
    comparator = gate["static_comparator"]
    if any(name not in ('BIG4', 'COMPACT4', 'ALL_ALLOWED4')
           for name in (comparator, policy['initial_mask'], policy['final_mask'])):
        raise ValueError('Policy selection must use matched four-worker arms only')
    specs = {"STOCK": {"cpus": None, "target_cpus": None, "phase_source": "none"},
             "STATIC": {"cpus": static[comparator]["cpus"], "target_cpus": None, "phase_source": "none"},
             "EXTERNAL": {"cpus": initial, "target_cpus": final, "phase_source": "external_proc"},
             "ORACLE": {"cpus": initial, "target_cpus": final, "phase_source": "internal_oracle"}}
    for spec in specs.values():
        spec.update(threads=4, threads_batch=4)
    return specs


def approval_bindings(plan):
    """Exact immutable documents an author approves before smoke begins."""
    return {'plan_sha256': digest(plan), 'maximum_session_s': plan['maximum_session_s'],
            'duration_rule_sha256': digest(plan['duration_rule']),
            'schedule_sha256': {tier: digest(schedule(plan, tier=tier))
                                for tier in ('FULL', 'STATIC_SIGNAL')},
            'runtime_fallback_policy_sha256': digest(plan['runtime_fallback_policy'])}


def check_approval(plan, approval, package_hash, git_report=None):
    if git_report is not None or approval.get('schema') == 'je-amd-git-approval-v1':
        if git_report is None:
            raise ValueError('Git-native approval requires verified checkout provenance')
        from . import git_delivery
        if git_report['source_sha256'] != package_hash:
            raise ValueError('Git source checksum mismatch')
        return git_delivery.check_approval(plan, approval, git_report)
    if approval.get("authorized") is not True or not approval.get("author_review_id"):
        raise ValueError("Separate explicit author execution approval is required")
    if approval.get("plan_sha256") != digest(plan) or approval.get("package_sha256") != package_hash:
        raise ValueError("Approval does not match frozen plan/package")
    if approval.get("maximum_session_s") != plan["maximum_session_s"]:
        raise ValueError("Session budget approval mismatch")
    if any(approval.get(key) != value for key, value in approval_bindings(plan).items()):
        raise ValueError('Approval duration rule/schedule/fallback bindings mismatch')
    authorized_fallback = approval.get('runtime_fallback_authorized', False)
    if type(authorized_fallback) is not bool or (authorized_fallback and plan['runtime_fallback_policy'].get('schema') != 'je-amd-offline-runtime-fallback-v1'):
        raise ValueError('Runtime fallback authorization not allowed by frozen policy')
    if 'selected_tier' in approval and approval['selected_tier'] != 'AUTO':
        raise ValueError('Approval cannot preselect or override the duration decision')
    return True


def _smoke_timings(plan, records):
    """Sanitize before hashing: scientific fields never enter duration inputs."""
    expected = {r['run_id']: r for r in schedule(plan, 'technical_smoke', tier='FULL')}
    if len(records) != 4 or len({r.get('run_id') for r in records}) != 4:
        raise ValueError('Duration decision requires exactly four unique technical smoke requests')
    result = []
    for record in records:
        slot = expected.get(record.get('run_id'))
        if (slot is None or any(record.get(k) != v for k, v in slot.items())
                or record.get('status') != 'ok'):
            raise ValueError('Duration decision accepts completed excluded technical smoke only')
        timing = {}
        for field in plan['duration_rule']['timing_fields']:
            if field == 'backend_total_elapsed_s' and field not in record:
                continue
            value = record.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError('Missing or invalid technical smoke timing')
            timing[field] = float(value)
        components = sum(timing[k] for k in ('startup_elapsed_s', 'warmup_elapsed_s', 'request_elapsed_s'))
        if max(components, timing.get('backend_total_elapsed_s', 0)) <= 0:
            raise ValueError('Nonpositive technical smoke duration')
        result.append({'run_id': record['run_id'], 'arm': record['arm'], **timing})
    if {r['arm'] for r in result} != set(plan['static_arms']):
        raise ValueError('Duration decision requires one technical smoke per static arm')
    return sorted(result, key=lambda r: r['run_id'])


def duration_decision(plan, smoke_records, elapsed_s):
    """Choose a complete frozen schedule using technical timing alone."""
    if isinstance(elapsed_s, bool) or not isinstance(elapsed_s, (float, int)) or not math.isfinite(elapsed_s) or elapsed_s < 0:
        raise ValueError('Invalid elapsed session time')
    timings = _smoke_timings(plan, smoke_records)
    rule = plan['duration_rule']
    costs = {}
    for record in timings:
        components = sum(record[k] for k in ('startup_elapsed_s', 'warmup_elapsed_s', 'request_elapsed_s'))
        costs[record['arm']] = ((1 + rule['timing_overhead_fraction']) *
                               max(components, record.get('backend_total_elapsed_s', components)) + plan['cooldown_s'])
    maximum = max(costs.values())
    estimates = {}
    for tier in ('FULL', 'STATIC_SIGNAL'):
        remaining = sum(maximum if slot['arm'] == 'INITIAL_SELECTED' or slot['cohort'] == 'policy_confirmatory'
                        else costs[slot['arm']] for slot in schedule(plan, tier=tier)
                        if slot['cohort'] != 'technical_smoke')
        estimates[tier] = {'remaining_requests': len(schedule(plan, tier=tier)) - 4,
                           'remaining_work_s': remaining,
                           'total_forecast_s': elapsed_s + remaining + rule['archive_reserve_s'] + rule['safety_reserve_s']}
    selected = next((tier for tier in ('FULL', 'STATIC_SIGNAL')
                     if estimates[tier]['total_forecast_s'] <= plan['maximum_session_s']), 'STOP')
    inputs = {'technical_smoke': timings, 'elapsed_s': float(elapsed_s)}
    bindings = approval_bindings(plan)
    return {'status': 'STOP' if selected == 'STOP' else 'DECIDED',
            'decision_tier': selected, 'selected_tier': selected,
            'inputs': inputs, 'inputs_digest': digest(inputs),
            'duration_rule_sha256': bindings['duration_rule_sha256'],
            'schedule_sha256': bindings['schedule_sha256'],
            'estimates': estimates, 'per_arm_allowance_s': costs,
            'archive_reserve_s': rule['archive_reserve_s'], 'safety_reserve_s': rule['safety_reserve_s'],
            'scientific_metrics_used': False, 'base_plan_changed': False}


def forecast(plan, smoke_records=None):
    """Observed smoke timings are safety forecasting only, never sample selection."""
    all_slots = schedule(plan)
    if not smoke_records:
        # These are explicit planning assumptions, not model-load measurements.
        return {"status": "PROVISIONAL", "maximum_requests": len(all_slots),
                "minimum_requests_if_policy_skipped": 48,
                "cooldown_s": len(all_slots) * plan["cooldown_s"],
                "range_minutes_including_setup": None,
                "model_loading_measured": False,
                "warning": "No measured startup/loading evidence for the new arms. Four excluded technical smoke requests select FULL, STATIC_SIGNAL, or STOP using the approved timing-only rule before exploration; the selected schedule is then immutable."}
    decision = duration_decision(plan, smoke_records, 0)
    costs = {**decision['per_arm_allowance_s'], 'INITIAL_SELECTED': max(decision['per_arm_allowance_s'].values())}
    return {"status": "OBSERVED_SMOKE_FORECAST", "remaining_s": decision['estimates']['FULL']['total_forecast_s'],
            "per_arm_allowance_s": costs, "maximum_requests": len(all_slots),
            "model_loading_measured": False, "startup_readiness_measured": True,
            "timing_basis": "Fresh-server health/readiness plus warmup/request or larger backend total; 25% timing overhead; all conditional policy slots reserved; archive and safety reserves included. Use duration_decision with elapsed session time for the binding tier choice."}
