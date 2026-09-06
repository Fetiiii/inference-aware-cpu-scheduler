#!/usr/bin/env python3
"""TASK-C03 minimal, portable generality runner.

C03 executes exactly two static placement arms and observes the frozen
external detector.  The diagnostic PHASE_MARK watcher is record-only: the
marker supplies offline ground truth and can never affect placement or the
detector state machine.  CPU classes are explicit operational labels supplied
by the experiment configuration; this module does not infer Intel P/E ranges.
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
EXPERIMENTS = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(EXPERIMENTS))

import bench_lib as bl  # noqa: E402
import c01_stock_scheduler as c01  # noqa: E402
import c02_external_oracle as c02  # noqa: E402
import run_once as ro  # noqa: E402


ARMS = ("BIG_ONLY", "ALL_CORES")
PATHS = ("CROSS_VENDOR", "FALLBACK_MODEL")
SMOKE_ROUNDS = 2
FULL_PILOT_ROUNDS = 6
DEFAULT_ORDER_SEED = 3304
FROZEN_INTERVAL_MS = 20.0
FROZEN_HI = 3000.0
FROZEN_LO = 2100.0
FROZEN_K = 2

CSV_FIELDS = (
    "round", "sequence_index", "global_sequence_index", "arm",
    "randomized_order_seed", "timestamp", "selected_c03_path",
    "detector_mode", "threads", "threads_batch", "cpu_mask", "ttft_ms",
    "itl_p50_ms", "itl_p95_ms", "itl_p99_ms", "decode_tps", "n_tokens",
    "temp_start_c", "temp_end_c", "total_migrations", "total_ctx_switches",
    "freq_big_avg_mhz", "freq_compact_avg_mhz", "freq_all_avg_mhz",
    "t_request_sent_ns", "t_internal_phase_ns", "t_marker_seen_ns",
    "t_external_detect_ns", "detect_vs_internal_ms", "environment_file",
    "detector_file", "phase_log", "server_log",
)


def parse_cpu_list(value):
    """Parse a portable Linux CPU-list expression into sorted unique IDs."""
    if value is None:
        raise ValueError("CPU list is required")
    cpus = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"empty CPU-list component in {value!r}")
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                raise ValueError(f"invalid CPU range: {part!r}")
            start, end = (int(piece) for piece in pieces)
            if start < 0 or end < start:
                raise ValueError(f"invalid CPU range: {part!r}")
            cpus.update(range(start, end + 1))
        else:
            cpu = int(part)
            if cpu < 0:
                raise ValueError("CPU IDs must be non-negative")
            cpus.add(cpu)
    if not cpus:
        raise ValueError("CPU list must not be empty")
    return sorted(cpus)


def cpu_list_text(cpus):
    return ",".join(str(cpu) for cpu in cpus)


def validate_topology(big_cpus, compact_cpus, online=None, allowed=None):
    """Validate caller-supplied operational core classes without inferring."""
    big = sorted(set(big_cpus))
    compact = sorted(set(compact_cpus))
    if not big or not compact:
        raise ValueError("both --big-cpus and --compact-cpus must be non-empty")
    overlap = sorted(set(big) & set(compact))
    if overlap:
        raise ValueError(f"big/compact CPU lists overlap: {overlap}")
    configured = set(big) | set(compact)
    if online is not None and not configured <= set(online):
        raise ValueError(
            f"configured CPUs are not all online: {sorted(configured - set(online))}"
        )
    if allowed is not None and not configured <= set(allowed):
        raise ValueError(
            "configured CPUs are outside the invoking process affinity: "
            f"{sorted(configured - set(allowed))}"
        )
    return {"big": big, "compact": compact, "all": big + compact}


def arm_specs(topology, threads_big, threads_all):
    return {
        "BIG_ONLY": {
            "threads": threads_big,
            "threads_batch": threads_big,
            "cpu_mask": list(topology["big"]),
        },
        "ALL_CORES": {
            "threads": threads_all,
            "threads_batch": threads_all,
            "cpu_mask": list(topology["all"]),
        },
    }


def build_schedule(rounds, order_seed):
    schedule = []
    global_index = 0
    for round_number in range(1, rounds + 1):
        round_seed = order_seed + round_number
        order = list(ARMS)
        random.Random(round_seed).shuffle(order)
        for sequence_index, arm in enumerate(order, 1):
            global_index += 1
            schedule.append({
                "round": round_number,
                "sequence_index": sequence_index,
                "global_sequence_index": global_index,
                "arm": arm,
                "randomized_order_seed": round_seed,
            })
    return schedule


def validate_detector_config(mode, interval_ms, hi, lo, k,
                             recalibration_label=None):
    frozen = (
        interval_ms == FROZEN_INTERVAL_MS
        and hi == FROZEN_HI
        and lo == FROZEN_LO
        and k == FROZEN_K
    )
    if mode == "zero_shot" and not frozen:
        raise ValueError(
            "zero_shot must preserve the frozen Intel detector exactly: "
            "interval=20 ms, hi=3000, lo=2100, k=2"
        )
    if mode == "recalibrated" and not recalibration_label:
        raise ValueError(
            "recalibrated mode requires --recalibration-label and may not be "
            "reported as zero-shot"
        )
    if interval_ms <= 0 or k <= 0 or lo >= hi:
        raise ValueError("detector requires interval>0, k>0, and lo<hi")
    return {
        "mode": mode,
        "interval_ms": interval_ms,
        "hi": hi,
        "lo": lo,
        "k": k,
        "frozen_intel_parameters_unchanged": frozen,
        "recalibration_label": recalibration_label,
    }


def validate_selected_path(path, fallback_model_family):
    if path not in PATHS:
        raise ValueError(f"selected C03 path must be one of {PATHS}")
    if path == "CROSS_VENDOR" and fallback_model_family:
        raise ValueError(
            "--fallback-model-family cannot be used with CROSS_VENDOR"
        )
    if path == "FALLBACK_MODEL" and not fallback_model_family:
        raise ValueError(
            "FALLBACK_MODEL requires --fallback-model-family to document the "
            "single changed generality axis"
        )
    return path


def run_stem(item):
    return (
        f"round_{item['round']:02d}_seq_{item['sequence_index']:02d}_"
        f"{item['arm'].lower()}"
    )


def schedule_from_round(plan, start_round):
    return [
        item for item in plan["schedule"]
        if item["round"] >= start_round
    ]


def validate_completed_prefix(outdir, schedule, start_round):
    if start_round <= 1:
        return
    root = Path(outdir)
    expected = [item for item in schedule if item["round"] < start_round]
    missing = [
        item for item in expected
        if not (root / "raw" / "runs" / f"{run_stem(item)}.json").exists()
    ]
    if missing:
        missing_desc = ", ".join(run_stem(item) for item in missing)
        raise RuntimeError(
            f"continuation from round {start_round} requires complete earlier rounds; "
            f"missing: {missing_desc}"
        )


def ensure_plan(outdir, args, topology, specs):
    path = Path(outdir) / "raw" / "plan.json"
    proposed = {
        "schema_version": 1,
        "task": "TASK-C03",
        "selected_c03_path": args.path,
        "rounds": args.rounds,
        "order_seed": args.order_seed,
        "arms": list(ARMS),
        "topology": topology,
        "arm_configs": specs,
        "detector": args.detector_config,
        "schedule": build_schedule(args.rounds, args.order_seed),
    }
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        same_protocol = (
            existing.get("task") == proposed["task"]
            and existing.get("selected_c03_path") == proposed["selected_c03_path"]
            and existing.get("order_seed") == proposed["order_seed"]
            and existing.get("arms") == proposed["arms"]
            and existing.get("topology") == proposed["topology"]
            and existing.get("arm_configs") == proposed["arm_configs"]
            and existing.get("detector") == proposed["detector"]
        )
        existing_schedule = existing.get("schedule", [])
        prefix_matches = (
            existing_schedule == proposed["schedule"][:len(existing_schedule)]
        )
        if not same_protocol or not prefix_matches:
            raise RuntimeError(
                f"existing {path} does not match requested protocol"
            )
        if existing.get("rounds", 0) > args.rounds:
            raise RuntimeError(
                f"existing {path} already contains more rounds than "
                "requested; do not shrink a persisted experiment plan"
            )
        if existing.get("rounds", 0) < args.rounds:
            # Explicitly approved full pilot extends the reviewed smoke;
            # all existing round entries remain byte-for-byte identical.
            c01.atomic_json(path, proposed)
        else:
            proposed = existing
    else:
        c01.atomic_json(path, proposed)
    return proposed


def read_cpu_identity():
    text = c01.read_text("/proc/cpuinfo") or ""
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in ("vendor_id", "model name", "Hardware") and key not in values:
            values[key] = value
    return {
        "vendor_id": values.get("vendor_id"),
        "model_name": values.get("model name") or values.get("Hardware"),
        "architecture": platform.machine(),
    }


def validate_hardware_for_path(path, cpu_identity):
    """Prevent silently running the selected axis on the wrong vendor."""
    vendor = cpu_identity.get("vendor_id")
    if path == "CROSS_VENDOR" and vendor == "GenuineIntel":
        raise RuntimeError(
            "CROSS_VENDOR was selected but this machine reports GenuineIntel; "
            "stop and use the intended second-vendor heterogeneous system"
        )
    if path == "FALLBACK_MODEL" and vendor not in (None, "GenuineIntel"):
        raise RuntimeError(
            "FALLBACK_MODEL is defined on the existing Intel platform, but "
            f"this machine reports vendor_id={vendor!r}"
        )


def verify_phase_mark_binary(path):
    """Reject a runtime whose executable/shared libraries lack the marker."""
    needle = b"PHASE_MARK"
    binary = Path(path).resolve()
    candidates = [binary, *sorted(binary.parent.glob("*.so*"))]
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        overlap = b""
        with resolved.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                data = overlap + chunk
                if needle in data:
                    return True
                overlap = data[-(len(needle) - 1):]
    raise RuntimeError(
        "diagnostic server binary does not contain PHASE_MARK; C03 stops "
        "before measurement rather than using weaker ground truth"
    )


def topology_metadata(topology):
    cpus = {}
    for label in ("big", "compact"):
        for cpu in topology[label]:
            base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
            cpus[str(cpu)] = {
                "operational_label": label,
                "core_id": bl._read_int(base / "core_id"),
                "package_id": bl._read_int(base / "physical_package_id"),
                "thread_siblings_list": c01.read_text(
                    base / "thread_siblings_list"
                ),
                "cpu_capacity": bl._read_int(
                    f"/sys/devices/system/cpu/cpu{cpu}/cpu_capacity"
                ),
                "cpuinfo_max_freq_khz": bl._read_int(
                    f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/cpuinfo_max_freq"
                ),
            }
    return {
        "classification": "explicit operational labels supplied by CLI",
        "exact_cpu_lists": topology,
        "cpus": cpus,
    }


def temperature_snapshot():
    sensors = {}
    for input_path in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
        raw = bl._read_int(input_path)
        if raw is None:
            continue
        label_path = input_path.replace("_input", "_label")
        label = c01.read_text(label_path) or Path(input_path).name
        hwmon = str(Path(input_path).parent)
        name = c01.read_text(Path(hwmon) / "name") or Path(hwmon).name
        sensors[f"{name}:{label}:{input_path}"] = raw / 1000.0
    preferred = [
        value for key, value in sensors.items()
        if any(term in key.lower() for term in ("package", "tctl", "tdie"))
    ]
    selected = max(preferred) if preferred else (max(sensors.values()) if sensors else None)
    return {"selected_c": selected, "readable_sensors_c": sensors}


class PortableFreqSampler:
    """Sample only caller-configured CPUs and retain operational labels."""

    def __init__(self, topology, interval_s=0.25):
        self.topology = topology
        self.interval_s = interval_s
        self.samples = {"big": [], "compact": [], "all": []}
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _read(cpus):
        values = []
        for cpu in cpus:
            value = bl._read_int(
                f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq"
            )
            if value is not None:
                values.append(value / 1000.0)
        return values

    def _loop(self):
        while not self._stop.is_set():
            for label in ("big", "compact", "all"):
                values = self._read(self.topology[label])
                if values:
                    self.samples[label].append(sum(values) / len(values))
            self._stop.wait(self.interval_s)

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="c03-portable-frequency"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self):
        def mean(values):
            return round(sum(values) / len(values), 1) if values else None
        return {
            "freq_big_avg_mhz": mean(self.samples["big"]),
            "freq_compact_avg_mhz": mean(self.samples["compact"]),
            "freq_all_avg_mhz": mean(self.samples["all"]),
            "freq_samples": {key: len(value) for key, value in self.samples.items()},
        }


class ObservationOnlyActuator:
    """ProcPhaseMonitor adapter with deliberately no affinity method."""

    record = None

    def capture_200ms_delta(self):
        return None


def build_observers(pid, log_path, offset, detector_config):
    """Build record-only observers; neither has an affinity callback."""
    monitor = c02.ProcPhaseMonitor(
        pid,
        interval_s=detector_config["interval_ms"] / 1000.0,
        hi=detector_config["hi"],
        lo=detector_config["lo"],
        k=detector_config["k"],
        actuator=ObservationOnlyActuator(),
        external_callback=None,
    )
    watcher = c02.PhaseMarkWatcher(
        log_path, offset, decision_callback=None
    )
    return monitor, watcher


def server_command(args, spec):
    return [
        "taskset", "-c", cpu_list_text(spec["cpu_mask"]),
        args.server_bin, "-m", args.model,
        "-t", str(spec["threads"]), "-tb", str(spec["threads_batch"]),
        "-c", str(args.ctx), "-b", str(args.batch),
        "-ub", str(args.ubatch), "-np", "1",
        "--host", ro.HOST, "--port", str(args.port),
    ]


def portable_preflight(topology):
    state = c01.sched_ext_state()
    if state not in ("disabled", "not-present"):
        raise RuntimeError(
            "C03 static-arm comparison requires sched_ext disabled; observed "
            f"{state!r}. The runner will not change scheduler state."
        )
    online = c01.online_cpus()
    allowed = sorted(os.sched_getaffinity(0))
    validate_topology(topology["big"], topology["compact"], online, allowed)
    return {"sched_ext_state": state, "online_cpus": online, "allowed_cpus": allowed}


def environment_record(args, topology, specs, preflight):
    return {
        "schema_version": 1,
        "task": "TASK-C03",
        "captured_at": c01.utc_timestamp(),
        "selected_c03_path": args.path,
        "single_generality_axis": (
            "CPU vendor/platform" if args.path == "CROSS_VENDOR"
            else "model architecture/family"
        ),
        "operational_label_disclosure": (
            "big and compact are experiment-supplied operational labels; "
            "they do not claim equivalence to Intel P/E core classes"
        ),
        "kernel": {
            "system": platform.system(), "release": platform.release(),
            "version": platform.version(), "machine": platform.machine(),
        },
        "cpu_identity": read_cpu_identity(),
        "preflight": preflight,
        "topology": topology_metadata(topology),
        "power": c01.power_metadata(),
        "temperature_at_capture": temperature_snapshot(),
        "llama_cpp": {
            "repository_commit": c01.git_head(ROOT / "llama.cpp"),
            "diagnostic_server_binary": c01.file_identity(args.server_bin, True),
        },
        "model": c01.file_identity(args.model, not args.skip_model_hash),
        "model_family": args.fallback_model_family,
        "prompt": c01.file_identity(args.prompt, True),
        "arm_configs": specs,
        "detector": args.detector_config,
        "protocol": {
            "rounds": args.rounds, "runs": args.rounds * len(ARMS),
            "order_seed": args.order_seed, "ctx": args.ctx,
            "batch": args.batch, "ubatch": args.ubatch,
            "n_predict": args.n_predict, "seed": args.seed,
            "cooldown_s": args.cooldown,
            "initial_cooldown_s": args.initial_cooldown,
            "diagnostic_marker_required": True,
            "marker_used_for_decisions": False,
            "external_detector_used_for_scheduling": False,
        },
    }


def register_environment(outdir, environment):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(outdir) / "raw" / "environment" / f"env_{stamp}.json"
    c01.atomic_json(path, environment)
    return str(path.relative_to(Path(outdir)))


def run_arm(args, item, spec, topology, environment_file):
    outdir = Path(args.outdir)
    stem = run_stem(item)
    run_path = outdir / "raw" / "runs" / f"{stem}.json"
    detector_path = outdir / "raw" / "detector" / f"{stem}.json"
    phase_path = outdir / "raw" / "phase_logs" / f"{stem}.json"
    server_log = outdir / "raw" / "server_logs" / f"{stem}.log"
    for path in (run_path, detector_path, phase_path, server_log):
        path.parent.mkdir(parents=True, exist_ok=True)

    log = server_log.open("wb")
    proc = monitor = watcher = freq = None
    try:
        proc = subprocess.Popen(
            server_command(args, spec), stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        ro.wait_for_health(args.port, proc)
        prompt = Path(args.prompt).read_text(encoding="utf-8")
        prompt_tokens = ro.tokenize(args.port, prompt)
        ro.stream_completion(args.port, {
            "prompt": "Warmup.", "n_predict": 1, "temperature": 0,
            "stream": True, "cache_prompt": False,
        })
        time.sleep(1.0)
        log.flush()
        warmup_offset = os.path.getsize(server_log)
        server_affinity = sorted(os.sched_getaffinity(proc.pid))
        if server_affinity != sorted(spec["cpu_mask"]):
            raise RuntimeError(
                f"{item['arm']} affinity mismatch: expected "
                f"{sorted(spec['cpu_mask'])}, observed {server_affinity}"
            )

        monitor, watcher = build_observers(
            proc.pid, server_log, warmup_offset, args.detector_config
        )
        monitor.start()
        watcher.start()
        time.sleep(0.3)

        temp_start = temperature_snapshot()
        sched_before = bl.sched_snapshot(proc.pid)
        freq = PortableFreqSampler(topology)
        freq.start()
        monitor_armed_ns = monitor.arm_for_request()
        t_sent, token_ts, text, _final = ro.stream_completion(args.port, {
            "prompt": prompt, "n_predict": args.n_predict,
            "temperature": 0.0, "seed": args.seed, "stream": True,
            "cache_prompt": False, "ignore_eos": True,
        })
        freq.stop()
        frequency = freq.summary()
        freq = None
        sched_after = bl.sched_snapshot(proc.pid)
        temp_end = temperature_snapshot()
        monitor.stop_flag.set()
        monitor.join(timeout=5)
        watcher.stop_flag.set()
        watcher.join(timeout=5)
        log.flush()

        markers = c02.parse_phase_markers_offline(server_log, warmup_offset)
        internal = c02.first_internal_boundary(markers, t_sent)
        if internal is None:
            raise RuntimeError(
                "diagnostic PHASE_MARK unavailable for the measured request; "
                "C03 stops rather than substituting first-token ground truth"
            )
        if watcher.t_internal_phase_ns != internal or watcher.t_marker_seen_ns is None:
            raise RuntimeError("live marker watcher did not preserve ground truth")

        metrics = c02.token_metrics(t_sent, token_ts)
        itl_values = metrics.pop("itl_ms")
        counters = bl.sched_delta(sched_before, sched_after)
        detect_vs_internal = (
            (monitor.external_detect_ns - internal) / 1e6
            if monitor.external_detect_ns is not None else None
        )
        detector_record = {
            "schema_version": 1, "task": "TASK-C03", **item,
            "selected_c03_path": args.path,
            "detector": args.detector_config,
            "decision_routing": "observation_only_no_affinity_callback",
            "marker_used_by_detector": False,
            "t_monitor_armed_ns": monitor_armed_ns,
            "t_request_sent_ns": t_sent,
            "t_internal_phase_ns": internal,
            "t_external_detect_ns": monitor.external_detect_ns,
            "detect_vs_internal_ms": detect_vs_internal,
            "summary": monitor.summary(),
            "samples": monitor.samples,
        }
        phase_record = {
            "schema_version": 1, "task": "TASK-C03", **item,
            "ground_truth_definition": (
                "first internally marked unbatched decode computation"
            ),
            "ground_truth_use": "offline_labeling_only",
            "marker_routed_to_policy": False,
            "markers_live": watcher.markers,
            "markers_offline_cross_check": markers,
            "t_internal_phase_ns": internal,
            "t_marker_seen_ns": watcher.t_marker_seen_ns,
        }
        c01.atomic_json(detector_path, detector_record)
        c01.atomic_json(phase_path, phase_record)

        record = {
            "schema_version": 1, "task": "TASK-C03", **item,
            "timestamp": c01.utc_timestamp(), "status": "ok",
            "selected_c03_path": args.path,
            "detector_mode": args.detector_config["mode"],
            "threads": spec["threads"],
            "threads_batch": spec["threads_batch"],
            "cpu_mask": cpu_list_text(spec["cpu_mask"]),
            "cpu_mask_list": list(spec["cpu_mask"]),
            "server_affinity_after_warmup": server_affinity,
            "prompt_tokens": prompt_tokens,
            **metrics, **frequency,
            "total_migrations": counters["migrations"],
            "total_ctx_switches": counters["ctx_switches"],
            "threads_seen": counters["threads_seen"],
            "temp_start_c": temp_start["selected_c"],
            "temp_end_c": temp_end["selected_c"],
            "temperature_start": temp_start,
            "temperature_end": temp_end,
            "t_request_sent_ns": t_sent,
            "t_internal_phase_ns": internal,
            "t_marker_seen_ns": watcher.t_marker_seen_ns,
            "t_external_detect_ns": monitor.external_detect_ns,
            "detect_vs_internal_ms": detect_vs_internal,
            "t_first_token_ns": token_ts[0],
            "t_last_token_ns": token_ts[-1],
            "token_ts_ns": token_ts,
            "itl_ms": itl_values,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "environment_file": environment_file,
            "detector_file": str(detector_path.relative_to(outdir)),
            "phase_log": str(phase_path.relative_to(outdir)),
            "server_log": str(server_log.relative_to(outdir)),
        }
        c01.atomic_json(run_path, record)
        return record
    finally:
        if freq is not None:
            freq.stop()
        if monitor is not None:
            monitor.stop_flag.set()
            monitor.join(timeout=2)
        if watcher is not None:
            watcher.stop_flag.set()
            watcher.join(timeout=2)
        if proc is not None:
            c01.terminate_process_group(proc)
        log.close()


def write_runs_csv(outdir):
    root = Path(outdir)
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((root / "raw" / "runs").glob("round_*.json"))
    ]
    path = root / "c03_runs.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def preserve_partial_artifacts(outdir, stem, stamp):
    """Move incomplete evidence aside before retrying; never overwrite it."""
    root = Path(outdir)
    candidates = (
        ("detector", ".json"),
        ("phase_logs", ".json"),
        ("server_logs", ".log"),
    )
    preserved = []
    for directory, suffix in candidates:
        source = root / "raw" / directory / f"{stem}{suffix}"
        if not source.exists():
            continue
        destination = (
            root / "raw" / directory / "failures"
            / f"{stem}_{stamp}{suffix}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        preserved.append(str(destination.relative_to(root)))
    return preserved


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run TASK-C03's two-arm minimal generality smoke"
    )
    parser.add_argument("--path", required=True, choices=PATHS)
    parser.add_argument("--big-cpus", required=True)
    parser.add_argument("--compact-cpus", required=True)
    parser.add_argument("--threads-big", required=True, type=int)
    parser.add_argument("--threads-all", required=True, type=int)
    parser.add_argument("--server-bin", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", default=str(HARNESS / "prompt_512.txt"))
    parser.add_argument("--fallback-model-family", default="")
    parser.add_argument("--outdir", default=str(ROOT / "results" / "conference_c03"))
    parser.add_argument("--rounds", type=int, default=SMOKE_ROUNDS)
    parser.add_argument(
        "--start-round", type=int, default=1,
        help="first absolute round to execute; --rounds remains total count",
    )
    parser.add_argument(
        "--full-pilot-approved", action="store_true",
        help="required for six rounds after explicit smoke checkpoint approval",
    )
    parser.add_argument("--order-seed", type=int, default=DEFAULT_ORDER_SEED)
    parser.add_argument(
        "--detector-mode", choices=("zero_shot", "recalibrated"),
        default="zero_shot",
    )
    parser.add_argument("--interval-ms", type=float, default=FROZEN_INTERVAL_MS)
    parser.add_argument("--hi", type=float, default=FROZEN_HI)
    parser.add_argument("--lo", type=float, default=FROZEN_LO)
    parser.add_argument("--k", type=int, default=FROZEN_K)
    parser.add_argument("--recalibration-label", default="")
    parser.add_argument("--ctx", type=int, default=2048)
    parser.add_argument("--batch", type=int, default=2048)
    parser.add_argument("--ubatch", type=int, default=512)
    parser.add_argument("--n-predict", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, default=8140)
    parser.add_argument("--initial-cooldown", type=int, default=30)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-model-hash", action="store_true")
    args = parser.parse_args(argv)

    if args.rounds not in (SMOKE_ROUNDS, FULL_PILOT_ROUNDS):
        parser.error(
            f"--rounds must be {SMOKE_ROUNDS} (smoke) or {FULL_PILOT_ROUNDS} (approved full pilot)"
        )
    if args.rounds == FULL_PILOT_ROUNDS and not args.full_pilot_approved:
        parser.error(
            "six-round C03 pilot requires explicit checkpoint approval and "
            "--full-pilot-approved"
        )
    if args.start_round < 1 or args.start_round > args.rounds:
        parser.error("--start-round must be between 1 and --rounds")
    if args.start_round > 1 and not args.resume:
        parser.error("continuation with --start-round requires --resume")
    try:
        args.path = validate_selected_path(args.path, args.fallback_model_family)
        args.detector_config = validate_detector_config(
            args.detector_mode, args.interval_ms, args.hi, args.lo, args.k,
            args.recalibration_label,
        )
        args.topology = validate_topology(
            parse_cpu_list(args.big_cpus), parse_cpu_list(args.compact_cpus)
        )
    except ValueError as exc:
        parser.error(str(exc))
    for name in (
        "threads_big", "threads_all", "ctx", "batch", "ubatch",
        "n_predict", "port",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.threads_all < args.threads_big:
        parser.error("--threads-all must be >= --threads-big")
    if args.initial_cooldown < 0 or args.cooldown < 0:
        parser.error("cooldowns must be non-negative")
    for name in ("server_bin", "model", "prompt"):
        if not Path(getattr(args, name)).exists():
            parser.error(f"--{name.replace('_', '-')} does not exist: {getattr(args, name)}")
    return args


def main(argv=None):
    args = parse_args(argv)
    verify_phase_mark_binary(args.server_bin)
    preflight = portable_preflight(args.topology)
    validate_hardware_for_path(args.path, read_cpu_identity())
    specs = arm_specs(args.topology, args.threads_big, args.threads_all)
    environment = environment_record(args, args.topology, specs, preflight)
    plan = ensure_plan(args.outdir, args, args.topology, specs)
    validate_completed_prefix(args.outdir, plan["schedule"], args.start_round)
    environment_file = register_environment(args.outdir, environment)
    if args.initial_cooldown:
        print(f"[C03] initial cooldown {args.initial_cooldown}s", flush=True)
        time.sleep(args.initial_cooldown)
    completed = failures = 0
    for item in schedule_from_round(plan, args.start_round):
        stem = run_stem(item)
        run_path = Path(args.outdir) / "raw" / "runs" / f"{stem}.json"
        if run_path.exists():
            if args.resume:
                print(f"  {stem}: existing result, skipped", flush=True)
                continue
            raise FileExistsError(f"{run_path} exists; use --resume")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        stale = preserve_partial_artifacts(args.outdir, stem, stamp)
        if stale:
            print(f"  {stem}: preserved incomplete artifacts: {stale}", flush=True)
        if completed and args.cooldown:
            print(f"[C03] cooldown {args.cooldown}s", flush=True)
            time.sleep(args.cooldown)
        print(
            f"[C03] round={item['round']} seq={item['sequence_index']} "
            f"arm={item['arm']} path={args.path}", flush=True,
        )
        try:
            record = run_arm(
                args, item, specs[item["arm"]], args.topology,
                environment_file,
            )
        except Exception as exc:
            failures += 1
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            preserved = preserve_partial_artifacts(args.outdir, stem, stamp)
            failure_path = (
                Path(args.outdir) / "raw" / "runs" / "failures"
                / f"{stem}_{stamp}.json"
            )
            c01.atomic_json(failure_path, {
                "schema_version": 1, "task": "TASK-C03", **item,
                "timestamp": c01.utc_timestamp(),
                "error_type": type(exc).__name__, "error": str(exc),
                "preserved_artifacts": preserved,
            })
            print(f"  FAILED: {exc}", flush=True)
        else:
            print(
                f"  ttft={record['ttft_ms']:.1f}ms "
                f"p95={record['itl_p95_ms']:.2f}ms "
                f"tps={record['decode_tps']:.2f} "
                f"detect={record['t_external_detect_ns'] is not None}",
                flush=True,
            )
        completed += 1
        write_runs_csv(args.outdir)
    print(f"[C03] run table -> {write_runs_csv(args.outdir)}", flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
