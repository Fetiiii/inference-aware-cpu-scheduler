"""Offline request-disjoint calibration. No runtime, affinity, or inference calls.

Reports describe operational marker agreement, never semantic precision/recall.
All protocol overrides must be frozen by the caller before collecting data.
"""
import hashlib
import itertools
import json
import math
import statistics

ARMS = ("ALL_ALLOWED4", "BIG4", "COMPACT4")  # lexical tie order
DEFAULTS = dict(interval_ms=20, max_gap_ms=50, window_ms=200, min_side_samples=3,
                quantiles=[.2, .35, .5, .65, .8], transforms=["identity", "log1p"],
                persistence=[2, 3], min_validation_eligible=6,
                min_coverage=1.0, min_advantage=.05)


def _hash(value):
    # Nonfinite raw samples are retained and hashed, although never fitted.
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _spec(spec):
    result = {**DEFAULTS, **spec}
    if result["interval_ms"] != 20:
        raise ValueError("sampling interval must remain 20 ms")
    return result


def _ids(runs, cohort):
    ids = [r["run_id"] for r in runs]
    if not runs or len(ids) != len(set(ids)):
        raise ValueError("nonempty requests with unique run_id required")
    if any(r["cohort"] != cohort for r in runs):
        raise ValueError("whole-request cohort violation")
    return sorted(ids)


def _value(sample, transform):
    x = sample.get("norm_ctx_per_cpu_s")
    if not isinstance(x, (float, int)) or not math.isfinite(x) or x < 0:
        return None
    return math.log1p(x) if transform == "log1p" else x


def zero_shot_model(spec=None):
    """Explicit source-domain reference, never relabelled as calibrated."""
    protocol = _spec(spec or {})
    return dict(schema="amd-oneshot-detector-v1", mode="zero_shot",
                transform="identity", lo=2100., hi=3000., k=2,
                polarity="rising", interval_ms=20, max_gap_ms=protocol["max_gap_ms"], train_ids=[],
                protocol_hash=_hash(protocol))


class DetectorState:
    """Constant-memory marker-blind detector shared by offline and live users.

    Starts prefill; invalid samples and nonpositive or excessive time gaps reset
    persistence. Confirmation time is the kth sample, never backdated.
    """

    def __init__(self, model):
        if "model_hash" in model and model["model_hash"] != _hash(
                {k: v for k, v in model.items() if k != "model_hash"}):
            raise ValueError("frozen model mutated")
        if model["transform"] not in ("identity", "log1p"):
            raise ValueError("unknown transform")
        if not model["lo"] < model["hi"] or model["k"] not in (2, 3):
            raise ValueError("invalid detector parameters")
        if model.get("interval_ms") != 20 or model["polarity"] not in ("rising", "falling"):
            raise ValueError("invalid fixed detector protocol")
        if not math.isfinite(model["max_gap_ms"]) or model["max_gap_ms"] <= 0:
            raise ValueError("positive finite max_gap_ms required")
        self.model = dict(model)
        self.state, self.streak, self.previous = "prefill", 0, None

    def feed(self, sample):
        model = self.model
        t = sample["t_ns"]
        if self.previous is not None:
            gap = t - self.previous
            if gap <= 0:
                self.streak = 0
                return None
            if gap > model["max_gap_ms"] * 1_000_000:
                self.streak = 0
        self.previous = t
        value = _value(sample, model["transform"])
        rising = model["polarity"] == "rising"
        high = (self.state == "prefill") == rising
        # Frozen C02 convention: equality at either boundary is not a crossing.
        crossed = value is not None and (value > model["hi"] if high else value < model["lo"])
        self.streak = self.streak + 1 if crossed else 0
        if self.streak >= model["k"]:
            self.state = "decode" if self.state == "prefill" else "prefill"
            self.streak = 0
            return dict(t_ns=t, state=self.state)
        return None


def detect_decisions(samples, model):
    """Replay samples through exactly the same state machine used live."""
    detector = DetectorState(model)
    return [event for sample in samples if (event := detector.feed(sample)) is not None]


def _report(runs, model, protocol):
    rows, offsets = [], []
    window = protocol["window_ms"] * 1_000_000
    for run in runs:
        marker = run.get("t_internal_phase_ns")
        valid = [s for s in run["samples"] if _value(s, model["transform"]) is not None]
        eligible = marker is not None and all(
            sum(s["t_ns"] < marker - window if side == "before" else
                s["t_ns"] > marker + window for s in valid) >= protocol["min_side_samples"]
            for side in ("before", "after"))
        events = detect_decisions(run["samples"], model)
        first_decode = next((i for i, e in enumerate(events) if e["state"] == "decode"), None)
        matched = bool(eligible and first_decode is not None and
                       abs(events[first_decode]["t_ns"] - marker) <= window)
        offset = (events[first_decode]["t_ns"] - marker) / 1e6 if matched else None
        if matched:
            offsets.append(offset)
        # Ineligible requests remain in the census; none of their events can
        # be credited as a valid marker match.
        spurious = len(events) - int(matched)
        rows.append(dict(run_id=run["run_id"], eligible=bool(eligible),
                         matched=matched, missed=bool(eligible and not matched),
                         spurious_events=spurious, transitions=events, offset_ms=offset,
                         t_internal_phase_ns=marker,
                         criterion_offset_ms=(events[first_decode]['t_ns']-marker)/1e6
                         if first_decode is not None and marker is not None else None))
    eligible_n = sum(r["eligible"] for r in rows)
    matched_n = sum(r["matched"] for r in rows)
    return dict(requests=len(rows), eligible_requests=eligible_n,
                ineligible_requests=len(rows)-eligible_n, matched_requests=matched_n,
                missed_requests=eligible_n-matched_n,
                coverage=matched_n/eligible_n if eligible_n else None,
                missed_rate=(eligible_n-matched_n)/eligible_n if eligible_n else None,
                spurious_requests=sum(r["spurious_events"] > 0 for r in rows),
                spurious_events=sum(r["spurious_events"] for r in rows),
                transitions=sum(len(r["transitions"]) for r in rows),
                spurious_request_rate=sum(r["spurious_events"] > 0 for r in rows)/len(rows),
                offsets_ms=offsets,
                median_offset_ms=statistics.median(offsets) if offsets else None,
                median_absolute_offset_ms=statistics.median(map(abs, offsets)) if offsets else None,
                precision=None, precision_undefined=True, recall=None, recall_undefined=True,
                denominator="whole requests; coverage uses marker-eligible requests, not semantic recall",
                thermal_warning=any(_thermal(r) for r in runs), rows=rows)


def _thermal(run):
    return bool(run.get("thermal_warning") or run.get("thermal_warned") or
                run.get("thermal_warnings") or run.get("throttle_warning"))


def _quantile(values, q):
    position = (len(values)-1)*q
    lo = int(position)
    return values[lo] + (values[min(lo+1, len(values)-1)]-values[lo])*(position-lo)


def fit(calibration_runs, spec):
    """Search only calibration requests; labels are used solely offline here."""
    runs = sorted(calibration_runs, key=lambda r: r["run_id"])
    ids, protocol = _ids(runs, "calibration"), _spec(spec)
    configuration = _single_configuration(runs)
    if len(runs) != 6:
        raise ValueError('exactly six calibration requests required')
    candidates = []
    for transform in protocol["transforms"]:
        if transform not in ("identity", "log1p"):
            raise ValueError("unknown transform")
        values = sorted(v for r in runs for s in r["samples"]
                        if (v := _value(s, transform)) is not None)
        if not values:
            continue
        thresholds = sorted(set(_quantile(values, q) for q in protocol["quantiles"]))
        for lo, hi in itertools.combinations(thresholds, 2):
            for polarity, k in itertools.product(("rising", "falling"), protocol["persistence"]):
                model = dict(schema="amd-oneshot-detector-v1", mode="recalibration",
                             transform=transform, lo=lo, hi=hi, polarity=polarity,
                             k=k, interval_ms=20, max_gap_ms=protocol["max_gap_ms"])
                report = _report(runs, model, protocol)
                tie = json.dumps(model, sort_keys=True)
                score = (report["spurious_events"], report["missed_requests"],
                         report["median_absolute_offset_ms"] if report["matched_requests"] else math.inf, tie)
                candidates.append((score, model))
    if not candidates:
        raise ValueError("no nondegenerate train-only threshold candidates")
    if not _report(runs, candidates[0][1], protocol)["eligible_requests"]:
        raise ValueError("no marker-eligible training requests")
    model = min(candidates, key=lambda c: c[0])[1]
    model.update(train_ids=ids, train_hash=_hash(runs), protocol_hash=_hash(protocol),
                 operating_configuration=configuration,
                 thermal_warning=any(_thermal(r) for r in runs))
    model["model_hash"] = _hash(model)
    return model


def evaluate(validation_runs, model, spec):
    """No fitting or retuning; rejects any train/validation ID intersection."""
    runs = list(validation_runs)
    ids, protocol = _ids(runs, "validation"), _spec(spec)
    configuration = _single_configuration(runs)
    if len(runs) != 6:
        raise ValueError('exactly six validation requests required')
    if model.get('mode') != 'zero_shot' and configuration != model.get('operating_configuration'):
        raise ValueError('validation operating configuration differs from calibration')
    if set(ids) & set(model["train_ids"]):
        raise ValueError("train/validation request overlap")
    if _hash(protocol) != model["protocol_hash"]:
        raise ValueError("protocol changed after model freeze")
    if "model_hash" in model and model["model_hash"] != _hash({k: v for k, v in model.items() if k != "model_hash"}):
        raise ValueError("frozen model mutated")
    report = _report(runs, model, protocol)
    report.update(validation_ids=ids, validation_hash=_hash(sorted(runs, key=lambda r: r['run_id'])),
                  operating_configuration=configuration, model_hash=model.get("model_hash"),
                  protocol_hash=model["protocol_hash"])
    return report


def _configuration(run):
    value = run.get('operating_configuration', run.get('arm'))
    if not value:
        raise ValueError('operating configuration required')
    return value


def _single_configuration(runs):
    configurations = {_configuration(r) for r in runs}
    if len(configurations) != 1:
        raise ValueError('pooled operating configurations forbidden')
    return configurations.pop()


def select_opportunity(exploration_runs, spec):
    """Select endpoints before collecting fresh single-mask detector training."""
    runs = sorted(exploration_runs, key=lambda r: r['run_id'])
    ids, protocol = _ids(runs, 'exploration'), _spec(spec)
    reasons = []
    rounds = sorted(set(r["round"] for r in runs), key=str)
    arms = {r['arm'] for r in runs}
    if (len(runs) != 8 or len(rounds) != 2 or arms != {*ARMS, 'ALL_CORES12'}
            or any(sum(r['round'] == rd and r['arm'] == arm for r in runs) != 1
                   for rd in rounds for arm in arms)):
        raise ValueError('complete two-round four-configuration exploration required')
    if any(_thermal(r) for r in runs):
        reasons.append('THERMAL_WARNING')
    for run in runs:
        if any(not isinstance(run.get(key), (int, float)) or not math.isfinite(run[key])
               or run[key] <= 0 for key in ('ttft_ms', 'itl_p95_ms')):
            raise ValueError('complete positive exploration metrics required')
        workers = 12 if run['arm'] == 'ALL_CORES12' else 4
        if any(run.get(key, workers) != workers for key in ('prefill_workers', 'decode_workers')):
            raise ValueError('exploration worker budget mismatch')
    selected = [r for r in runs if r["arm"] in ARMS]
    def median(arm, key, round_id=None):
        values = [r[key] for r in selected if r["arm"] == arm and
                  (round_id is None or r["round"] == round_id)]
        if not values or any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("complete positive metrics required for every arm/round")
        return statistics.median(values)
    for round_id in rounds:
        for arm in ARMS:
            for key in ("ttft_ms", "itl_p95_ms"):
                median(arm, key, round_id)
    prefill = min(ARMS, key=lambda a: (median(a, "ttft_ms"), a))
    decode = min(ARMS, key=lambda a: (median(a, "itl_p95_ms"), a))
    min_ttft = median(prefill, "ttft_ms")
    min_itl = median(decode, "itl_p95_ms")
    comparator = min(ARMS, key=lambda a: (math.sqrt(median(a, "ttft_ms")/min_ttft *
                                                  median(a, "itl_p95_ms")/min_itl), a))
    if prefill == decode:
        reasons.append("IDENTICAL_ENDPOINTS")
    advantages = dict(prefill=1-median(prefill, "ttft_ms")/median(decode, "ttft_ms"),
                      decode=1-median(decode, "itl_p95_ms")/median(prefill, "itl_p95_ms"))
    if min(advantages.values()) < protocol["min_advantage"]:
        reasons.append("INSUFFICIENT_PHASE_ADVANTAGE")
    if any(median(prefill, "ttft_ms", rd) >= median(decode, "ttft_ms", rd) or
           median(decode, "itl_p95_ms", rd) >= median(prefill, "itl_p95_ms", rd) for rd in rounds):
        reasons.append("ROUND_DIRECTION_DISAGREEMENT")
    result = dict(status="NO_OPPORTUNITY" if reasons else "OPPORTUNITY", reasons=reasons,
                policy=None if reasons else dict(initial_mask=prefill, final_mask=decode,
                                                 prefill_workers=4, decode_workers=4),
                initial_mask='ALL_ALLOWED4' if reasons else prefill,
                static_comparator=comparator, training_advantages=advantages,
                exploration_ids=ids, exploration_hash=_hash(runs),
                protocol_hash=_hash(protocol),
                alternatives_allowed=False, retuning_allowed=False)
    result['selection_hash'] = _hash(result)
    return result


def gate_policy(exploration_runs, validation_report, model, spec):
    selection = select_opportunity(exploration_runs, spec)
    protocol = _spec(spec)
    DetectorState(model)
    if (model.get('mode') != 'recalibration' or model.get('protocol_hash') != _hash(protocol)
            or validation_report.get('protocol_hash') != _hash(protocol)
            or validation_report.get('model_hash') != model.get('model_hash')):
        raise ValueError('validation report does not match frozen calibrated model/protocol')
    cohorts = [set(selection['exploration_ids']), set(model['train_ids']),
               set(validation_report['validation_ids'])]
    if any(a & b for a, b in itertools.combinations(cohorts, 2)):
        raise ValueError('exploration/calibration/validation request overlap')
    reasons = list(selection['reasons'])
    if (model.get('operating_configuration') != selection['initial_mask'] or
            validation_report.get('operating_configuration') != selection['initial_mask']):
        reasons.append('OPERATING_CONFIGURATION_MISMATCH')
    if model.get('thermal_warning') or validation_report['thermal_warning']:
        reasons.append('THERMAL_WARNING')
    if len(model['train_ids']) != 6 or not model.get('train_hash') or not validation_report.get('validation_hash'):
        reasons.append('INCOMPLETE_COHORT_PROVENANCE')
    if validation_report['eligible_requests'] != 6 or len(validation_report['validation_ids']) != 6:
        reasons.append('INSUFFICIENT_ELIGIBLE_VALIDATION')
    if validation_report['coverage'] != 1.0:
        reasons.append('INSUFFICIENT_COVERAGE')
    if validation_report['spurious_events'] != 0:
        reasons.append('SPURIOUS_VALIDATION_REQUESTS')
    return {**selection, 'status': 'NOT_ELIGIBLE' if reasons else 'ELIGIBLE',
            'reasons': sorted(set(reasons)), 'policy': None if reasons else selection['policy'],
            'model_hash': model['model_hash']}


def transfer_report(records, model, spec):
    """Evaluate frozen detector on fresh static confirmation, never retune."""
    runs = sorted(records, key=lambda r: r['run_id'])
    ids = _ids(runs, 'static_confirmatory')
    protocol = _spec(spec)
    DetectorState(model)
    if _hash(protocol) != model['protocol_hash']:
        raise ValueError('protocol changed after model freeze')
    if set(ids) & set(model['train_ids']):
        raise ValueError('transfer/calibration request overlap')
    groups = {}
    for run in runs:
        groups.setdefault(_configuration(run), []).append(run)
    if set(groups) != {*ARMS, 'ALL_CORES12'} or any(len(group) != 6 for group in groups.values()):
        raise ValueError('transfer requires six static-confirmatory requests per four configurations')
    return dict(model_hash=model.get('model_hash'), records_hash=_hash(runs),
                retuning_allowed=False, reports={configuration: {
                    **_report(group, model, protocol),
                    'domain': 'initial_configuration_matched' if configuration == model.get('operating_configuration')
                              else ('worker_count_and_eligibility_confound' if configuration == 'ALL_CORES12'
                                    else 'other_four_worker_placement'),
                    'run_ids': sorted(r['run_id'] for r in group)}
                    for configuration, group in sorted(groups.items())})
