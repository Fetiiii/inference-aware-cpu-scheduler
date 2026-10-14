#!/usr/bin/env python3
"""TASK-C04B bounded Intel worker-tail diagnostic runner.

This runner executes exactly two counterbalanced rounds of P8_ONLY and
PE_ALLOWED_T8_TB16.  It never changes scheduler, governor, EPP, turbo, or
other system settings.  C04 instrumentation is armed only after warmup and
records a fixed 16-graph unbatched decode window in the diagnostic runtime.
"""

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
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
import thread_residency as tr  # noqa: E402


TASK = "TASK-C04B"
ARMS = ("P8_ONLY", "PE_ALLOWED_T8_TB16")
P_CPUS = tuple(bl.P_CORES_NOSMT)
E_CPUS = tuple(bl.E_CORES)
SMOKE_ROUNDS = 2
ORDER_SEED = 4404
SCHEDULE_DESIGN = "counterbalanced_two_rounds_v1"
TRACE_WINDOW_GRAPHS = 16
TRACE_CAPACITY = 131072
PINNED_LLAMA_COMMIT = "571d0d540df04f25298d0e159e520d9fc62ed121"
MODEL_SHA256 = "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8"
PROMPT_SHA256 = "27f3f8ba7b73d3df1337549237a8e9f4ccf57e0551908cc93ce8a183480751b8"
PHASE_PATCH = ROOT / "conference" / "diagnostic" / "llama_cpp_phase_mark.patch"
C04_PATCH = ROOT / "conference" / "diagnostic" / "c04_worker_tail.patch"
MARK_RE = re.compile(r"PHASE_MARK batched=(\d+) t_mono_ns=(\d+)")

CSV_FIELDS = (
    "round", "sequence_index", "global_sequence_index", "arm",
    "randomized_order_seed", "schedule_design", "round_order_source",
    "timestamp", "status", "thermal_status",
    "threads", "threads_batch", "cpu_mask", "trace_window_graphs",
    "trace_event_count", "trace_dropped_events", "ttft_ms",
    "itl_p50_ms", "itl_p95_ms", "itl_p99_ms", "decode_tps", "n_tokens",
    "total_migrations", "total_ctx_switches", "temp_start_c", "temp_end_c",
    "thermal_throttle_delta", "freq_p_avg_mhz", "freq_p_busy_mhz",
    "freq_e_avg_mhz", "t_request_sent_ns", "t_internal_phase_ns",
    "t_first_token_ns", "t_last_token_ns", "environment_file",
    "trace_file", "token_file", "server_log",
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def cpu_list_text(cpus):
    return ",".join(str(cpu) for cpu in cpus)


def verify_workload(args):
    identities = {}
    for name, expected in (("model", MODEL_SHA256), ("prompt", PROMPT_SHA256)):
        identity = c01.file_identity(getattr(args, name), include_hash=True)
        if identity.get("sha256") != expected:
            raise RuntimeError(f"C04B {name} differs from frozen Intel C01/C02 workload")
        identities[name] = identity
    return identities


def arm_specs():
    wide = list(P_CPUS + E_CPUS)
    return {
        "P8_ONLY": {
            "threads": 8,
            "threads_batch": 8,
            "cpu_mask": list(P_CPUS),
        },
        "PE_ALLOWED_T8_TB16": {
            "threads": 8,
            "threads_batch": 16,
            "cpu_mask": wide,
        },
    }


def build_schedule(rounds=SMOKE_ROUNDS, order_seed=ORDER_SEED):
    if rounds != SMOKE_ROUNDS:
        raise ValueError("C04B implementation checkpoint permits exactly 2 rounds")
    if order_seed != ORDER_SEED:
        raise ValueError(f"C04B order seed is frozen at {ORDER_SEED}")
    schedule = []
    global_index = 0
    first_order = list(ARMS)
    random.Random(order_seed).shuffle(first_order)
    for round_number in range(1, rounds + 1):
        order = first_order if round_number == 1 else list(reversed(first_order))
        for sequence_index, arm in enumerate(order, 1):
            global_index += 1
            schedule.append({
                "round": round_number,
                "sequence_index": sequence_index,
                "global_sequence_index": global_index,
                "arm": arm,
                "randomized_order_seed": order_seed,
                "schedule_design": SCHEDULE_DESIGN,
                "round_order_source": "seeded_shuffle" if round_number == 1 else "reverse_round_1",
            })
    return schedule


def server_command(args, spec):
    return [
        "taskset", "-c", cpu_list_text(spec["cpu_mask"]),
        args.server_bin,
        "-m", args.model,
        "-t", str(spec["threads"]),
        "-tb", str(spec["threads_batch"]),
        "-c", str(args.ctx),
        "-b", str(args.batch),
        "-ub", str(args.ubatch),
        "-np", "1",
        "-ngl", "0",
        "--host", ro.HOST,
        "--port", str(args.port),
    ]


def trace_environment(base_environment, trace_path=None, arm_path=None,
                      enabled=True):
    """Return the exact server environment; disabled mode adds no C04 keys."""
    environment = dict(base_environment)
    for key in ("C04_WORKER_TAIL_TRACE_PATH", "C04_WORKER_TAIL_ARM_FILE"):
        environment.pop(key, None)
    if enabled:
        if trace_path is None or arm_path is None:
            raise ValueError("enabled trace requires trace and arm paths")
        environment["C04_WORKER_TAIL_TRACE_PATH"] = str(Path(trace_path).resolve())
        environment["C04_WORKER_TAIL_ARM_FILE"] = str(Path(arm_path).resolve())
    return environment


def create_trace_arm_file(path, enabled=True):
    """Arm one measured request; exclusive creation prevents stale reuse."""
    path = Path(path)
    if not enabled:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write("C04_WORKER_TAIL_ARM\n")
    return True


def read_trace(path):
    """Read raw JSONL without hiding overflow or incomplete grouping."""
    records = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trace JSON at line {line_number}") from exc
    if not records or records[0].get("record_type") != "meta":
        raise ValueError("trace must begin with one metadata record")
    if any(record.get("record_type") != "event" for record in records[1:]):
        raise ValueError("trace contains an unexpected non-event record")
    return records[0], records[1:]


def trace_validation_errors(meta, events):
    errors = []
    if meta.get("trace_window_graphs") != TRACE_WINDOW_GRAPHS:
        errors.append("trace window is not the frozen 16 graphs")
    if meta.get("buffer_capacity") != TRACE_CAPACITY:
        errors.append("trace buffer capacity differs from the frozen build")
    if meta.get("event_count") != len(events):
        errors.append("trace metadata event_count does not match JSONL events")
    if meta.get("dropped_events", 0) != 0:
        errors.append(f"trace dropped {meta.get('dropped_events')} events")
    if not events:
        errors.append("trace contains no MUL_MAT worker events")
    epochs = {event.get("epoch") for event in events}
    if epochs and epochs != set(range(1, TRACE_WINDOW_GRAPHS + 1)):
        errors.append("trace does not contain every frozen decode epoch 1..16")
    grouped = defaultdict(list)
    for event in events:
        required = ("epoch", "node_index", "worker_index", "active_workers",
                    "t_start_ns", "t_arrival_ns", "t_exit_ns", "cpu_start",
                    "cpu_arrival", "cpu_exit", "op", "src0_type", "is_q4_k",
                    "migrated_start_to_arrival", "ne", "src0_ne", "src1_ne")
        if any(key not in event for key in required):
            errors.append("trace event missing required fields")
            continue
        if not (0 < event["t_start_ns"] <= event["t_arrival_ns"] <= event["t_exit_ns"]):
            errors.append("trace event timestamps are incomplete or unordered")
        if event["op"] != "MUL_MAT":
            errors.append("trace includes a non-MUL_MAT event")
        if bool(event["is_q4_k"]) != (event["src0_type"].lower() == "q4_k"):
            errors.append("trace Q4_K label does not match src0 type")
        if any(len(event[key]) != 4 for key in ("ne", "src0_ne", "src1_ne")):
            errors.append("trace tensor shape must have four dimensions")
        grouped[(event["epoch"], event["node_index"])].append(event)
    for key, group in grouped.items():
        workers = [event["worker_index"] for event in group]
        if len(group) != 8 or set(workers) != set(range(8)) or any(event["active_workers"] != 8 for event in group):
            errors.append(f"node {key} does not contain the eight decode workers exactly once")
    if len(events) > TRACE_CAPACITY:
        errors.append("trace exceeds fixed buffer capacity")
    return list(dict.fromkeys(errors))


def classify_cpu(cpu):
    if cpu in P_CPUS or cpu in set(range(1, 16, 2)):
        return "P"
    if cpu in E_CPUS:
        return "E"
    return "UNKNOWN"


def classify_worker_event(event):
    start = event.get("cpu_start")
    arrival = event.get("cpu_arrival")
    migrated = bool(event.get("migrated_start_to_arrival")) or start != arrival
    if migrated:
        return "migrated"
    cpu_class = classify_cpu(arrival)
    if cpu_class == "P":
        return "stable_P"
    if cpu_class == "E":
        return "stable_E"
    return "stable_unknown"


def cpu_identity():
    vendor = model = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("vendor_id") and vendor is None:
                vendor = line.split(":", 1)[1].strip()
            elif line.startswith("model name") and model is None:
                model = line.split(":", 1)[1].strip()
            if vendor and model:
                break
    except OSError:
        pass
    return {"vendor_id": vendor, "model_name": model}


def validate_intel_topology():
    identity = cpu_identity()
    if identity["vendor_id"] != "GenuineIntel":
        raise RuntimeError(f"C04B requires Intel; observed {identity}")
    if "i7-14650HX" not in (identity.get("model_name") or ""):
        raise RuntimeError(f"C04B requires the original i7-14650HX; observed {identity}")
    allowed = set(os.sched_getaffinity(0))
    online = set(c01.online_cpus())
    selected = set(P_CPUS + E_CPUS)
    if not selected <= online:
        raise RuntimeError(f"C04B CPUs offline: {sorted(selected - online)}")
    if not selected <= allowed:
        raise RuntimeError(
            f"C04B CPUs outside invoking affinity: {sorted(selected - allowed)}"
        )
    cpu_core, core_cpus = tr.read_topology()
    p_core_ids = []
    for cpu in P_CPUS:
        core_id, is_p = cpu_core.get(cpu, (None, False))
        if not is_p or core_id is None:
            raise RuntimeError(f"configured P representative {cpu} is not P")
        siblings = core_cpus.get(core_id, [])
        if len(siblings) != 2 or cpu != min(siblings):
            raise RuntimeError(
                f"P representative {cpu} is not one-per-core: siblings={siblings}"
            )
        p_core_ids.append(core_id)
    if len(set(p_core_ids)) != 8:
        raise RuntimeError("P8 list does not represent eight physical P cores")
    for cpu in E_CPUS:
        _core_id, is_p = cpu_core.get(cpu, (None, True))
        if is_p:
            raise RuntimeError(f"configured E CPU {cpu} is not E")
    return {
        "identity": identity,
        "online_cpus": sorted(online),
        "allowed_cpus": sorted(allowed),
        "p_physical_representatives": list(P_CPUS),
        "e_cpus": list(E_CPUS),
    }


def top_cpu_processes(limit=12):
    # Track this exact sampler PID; excluding every process named ps would
    # hide genuine external CPU load instead of only our self-observation.
    with subprocess.Popen(
        ["ps", "-eo", "pid=,comm=,%cpu=", "--sort=-%cpu"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as sampler:
        stdout, stderr = sampler.communicate()
        if sampler.returncode:
            raise subprocess.CalledProcessError(
                sampler.returncode, sampler.args, output=stdout, stderr=stderr,
            )
        excluded_pids = {os.getpid(), sampler.pid}
    rows = []
    for line in stdout.splitlines():
        pieces = line.split(None, 2)
        if len(pieces) != 3:
            continue
        try:
            pid, command, cpu_pct = int(pieces[0]), pieces[1], float(pieces[2])
        except ValueError:
            continue
        if pid in excluded_pids:
            continue
        rows.append({"pid": pid, "comm": command, "cpu_pct": cpu_pct})
        if len(rows) >= limit:
            break
    return rows


def interference_warnings(processes):
    """Conservative loud gate for obvious CPU-heavy experiment conflicts."""
    patterns = re.compile(
        r"(llama|loadgen|stress|cc1|gcc|g\+\+|clang|make|ninja|cmake|"
        r"rustc|cargo|pytest|updatedb|index|packagekit|pacman|codex)",
        re.IGNORECASE,
    )
    warnings = []
    for process in processes:
        if patterns.search(process["comm"]) and process["cpu_pct"] >= 10.0:
            warnings.append(
                f"pid={process['pid']} comm={process['comm']} "
                f"cpu={process['cpu_pct']:.1f}%"
            )
        elif process["cpu_pct"] >= 75.0:
            warnings.append(
                f"pid={process['pid']} comm={process['comm']} "
                f"cpu={process['cpu_pct']:.1f}% (unclassified heavy process)"
            )
    return warnings


def machine_state():
    return {
        "captured_at": c01.utc_timestamp(),
        "load_average_1_5_15": list(os.getloadavg()),
        "top_cpu_processes": top_cpu_processes(),
        "power": {
            **c01.power_metadata(),
            "intel_pstate_status": c01.read_text(
                "/sys/devices/system/cpu/intel_pstate/status"
            ),
        },
        "package_temp_c": bl.package_temp_c(),
        "thermal_throttle_counters": c02.thermal_throttle_snapshot() or None,
        "runtime_environment": runtime_environment(),
    }


def runtime_environment():
    return {key: value for key, value in os.environ.items()
            if key.startswith(("OMP_", "GOMP_", "KMP_")) and value}


def validate_runtime_environment():
    overrides = runtime_environment()
    if overrides:
        raise RuntimeError("C04B requires default OpenMP behavior; unset runtime overrides: "
                           + ", ".join(sorted(overrides)))


def verify_build_manifest(path, server_bin):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("source_commit") != PINNED_LLAMA_COMMIT:
        raise RuntimeError("C04 build manifest has the wrong llama.cpp commit")
    expected_patches = {
        "phase_mark": sha256_file(PHASE_PATCH),
        "c04_worker_tail": sha256_file(C04_PATCH),
    }
    if manifest.get("patch_sha256") != expected_patches:
        raise RuntimeError("C04 build manifest patch hashes do not match repository")
    binary = Path(server_bin).resolve()
    binary_record = manifest.get("binary", {})
    if Path(binary_record.get("path", "")).resolve() != binary:
        raise RuntimeError("--server-bin does not match C04 build manifest")
    if binary_record.get("sha256") != sha256_file(binary):
        raise RuntimeError("C04 diagnostic binary hash does not match manifest")
    libraries = manifest.get("shared_libraries", [])
    if not libraries:
        raise RuntimeError("C04 build manifest must include shared-library hashes")
    for library in libraries:
        library_path = Path(library["path"])
        if not library_path.is_file() or sha256_file(library_path) != library["sha256"]:
            raise RuntimeError(f"C04 shared-library hash mismatch: {library_path.name}")
    expected_cache = {"CMAKE_BUILD_TYPE": "Release", "BUILD_SHARED_LIBS": "ON",
                      "GGML_NATIVE": "ON", "GGML_OPENMP": "ON",
                      "GGML_CPU_REPACK": "ON", "GGML_LLAMAFILE": "ON",
                      "LLAMA_BUILD_UI": "OFF", "LLAMA_USE_PREBUILT_UI": "OFF"}
    if any(manifest.get("cmake_cache", {}).get(key) != value for key, value in expected_cache.items()):
        raise RuntimeError("C04 build manifest does not preserve frozen Intel CMake configuration")
    embedded = binary_record.get("embedded_source_commit", "")
    if len(embedded) < 7 or not PINNED_LLAMA_COMMIT.startswith(embedded):
        raise RuntimeError("C04 manifest embedded llama.cpp revision does not match pinned source")
    capabilities = set(manifest.get("capabilities", []))
    if {"PHASE_MARK", "C04_WORKER_TAIL"} - capabilities:
        raise RuntimeError("manifest does not attest PHASE_MARK and C04 trace")
    return manifest


def protect_historical_outputs(outdir):
    output = Path(outdir).resolve()
    results_root = (ROOT / "results").resolve()
    c04_root = results_root / "conference_c04"
    if output.is_relative_to(results_root) and not output.is_relative_to(c04_root):
        raise RuntimeError("C04 output must not write to historical result directories")
    if output == c04_root / "recon" or output.is_relative_to(c04_root / "recon"):
        raise RuntimeError("C04A reconnaissance is frozen; choose worker_tail output")
    return output


def ensure_plan(outdir, specs):
    protect_historical_outputs(outdir)
    outdir = Path(outdir)
    plan_path = outdir / "plan.json"
    proposed = {
        "schema_version": 1,
        "task": TASK,
        "rounds": SMOKE_ROUNDS,
        "run_count": SMOKE_ROUNDS * len(ARMS),
        "order_seed": ORDER_SEED,
        "schedule_design": SCHEDULE_DESIGN,
        "trace_window_graphs": TRACE_WINDOW_GRAPHS,
        "trace_capacity": TRACE_CAPACITY,
        "model_sha256": MODEL_SHA256,
        "prompt_sha256": PROMPT_SHA256,
        "arms": list(ARMS),
        "arm_configs": specs,
        "schedule": build_schedule(),
    }
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != proposed:
            raise RuntimeError(f"existing {plan_path} is incompatible")
    else:
        if outdir.exists() and any(outdir.iterdir()):
            raise RuntimeError(
                f"{outdir} contains data but no compatible C04B plan; refusing"
            )
        c01.atomic_json(plan_path, proposed)
    existing_runs = list((outdir / "raw" / "runs").glob("round_*.json"))
    if existing_runs:
        raise RuntimeError("C04B does not overwrite or resume measured runs")
    return proposed


def register_environment(outdir, value):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(outdir) / "environment" / f"env_{stamp}.json"
    c01.atomic_json(path, value)
    return str(path.relative_to(Path(outdir)))


def run_stem(item):
    return (
        f"round_{item['round']:02d}_seq_{item['sequence_index']:02d}_"
        f"{item['arm'].lower()}"
    )


def wait_for_trace(path, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if Path(path).exists() and Path(path).stat().st_size > 0:
            return
        time.sleep(0.05)
    raise RuntimeError(f"C04 trace was not dumped: {path}")


def run_arm(args, item, spec, environment_file):
    outdir = Path(args.outdir)
    stem = run_stem(item)
    run_path = outdir / "raw" / "runs" / f"{stem}.json"
    token_path = outdir / "raw" / "tokens" / f"{stem}.json"
    trace_path = outdir / "raw" / "traces" / f"{stem}.jsonl"
    arm_path = outdir / "raw" / "traces" / f"{stem}.arm"
    server_log_path = outdir / "raw" / "server_logs" / f"{stem}.log"
    for path in (run_path, token_path, trace_path, arm_path, server_log_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    before_run_state = machine_state()
    conflicts = interference_warnings(before_run_state["top_cpu_processes"])
    if conflicts:
        print("\n*** C04B PRECHECK WARNING: CPU-HEAVY WORKLOAD DETECTED ***")
        for conflict in conflicts:
            print(f"  {conflict}")
        print("Stop the workload and rerun; C04B will not start this arm.\n")
        raise RuntimeError("CPU interference gate failed")

    log_handle = server_log_path.open("xb")
    proc = None
    freq = None
    try:
        proc = subprocess.Popen(
            server_command(args, spec),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=trace_environment(os.environ, trace_path, arm_path),
        )
        ro.wait_for_health(args.port, proc)
        prompt = Path(args.prompt).read_text(encoding="utf-8")
        prompt_tokens = ro.tokenize(args.port, prompt)
        ro.stream_completion(args.port, {
            "prompt": "Warmup.", "n_predict": 1, "temperature": 0.0,
            "seed": args.seed, "stream": True, "cache_prompt": False,
            "ignore_eos": True,
        })
        time.sleep(1.0)
        log_handle.flush()
        warmup_offset = os.path.getsize(server_log_path)

        # This file is the only external arming action. The diagnostic runtime
        # consumes it and starts collection only at the first unbatched graph.
        create_trace_arm_file(arm_path)

        temp_start = bl.package_temp_c()
        throttle_before = c02.thermal_throttle_snapshot()
        sched_before = bl.sched_snapshot(proc.pid)
        freq = bl.FreqSampler(busy_n=8)
        freq.start()

        t_sent, token_ts, text, _final = ro.stream_completion(args.port, {
            "prompt": prompt,
            "n_predict": args.n_predict,
            "temperature": 0.0,
            "seed": args.seed,
            "stream": True,
            "cache_prompt": False,
            "ignore_eos": True,
        })

        freq.stop()
        frequency = freq.summary()
        freq = None
        sched_after = bl.sched_snapshot(proc.pid)
        throttle_after = c02.thermal_throttle_snapshot()
        temp_end = bl.package_temp_c()
        log_handle.flush()
        try:
            wait_for_trace(trace_path)
            meta, events = read_trace(trace_path)
            trace_errors = trace_validation_errors(meta, events)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            meta, events = {}, []
            trace_errors = [f"unreadable trace: {exc}"]
        markers = c02.parse_phase_markers_offline(server_log_path, warmup_offset)
        internal_boundary = c02.first_internal_boundary(markers, t_sent)
        if internal_boundary is None:
            trace_errors.append("measured-request internal PHASE_MARK missing")
        elif any(event.get("t_start_ns", 0) < internal_boundary for event in events):
            trace_errors.append("trace contains events before measured-request internal boundary")
        allowed_mask = set(spec["cpu_mask"])
        if any(event.get(key) not in allowed_mask for event in events
               for key in ("cpu_start", "cpu_arrival", "cpu_exit")):
            trace_errors.append("trace sampled a CPU outside the arm's allowed mask")

        metrics = c02.token_metrics(t_sent, token_ts)
        itl_values = metrics.pop("itl_ms")
        sched_delta = bl.sched_delta(sched_before, sched_after)
        throttle = c02.thermal_throttle_delta(throttle_before, throttle_after)
        throttle_total = throttle.get("total", 0) if throttle else None
        thermal_status = (
            "THERMAL_WARNING" if throttle_total is not None and throttle_total > 0
            else "OK" if throttle_total is not None else "UNREADABLE"
        )

        c01.atomic_json(token_path, {
            "schema_version": 1,
            "task": TASK,
            "arm": item["arm"],
            "t_request_sent_ns": t_sent,
            "t_first_token_ns": token_ts[0],
            "t_last_token_ns": token_ts[-1],
            "token_ts_ns": token_ts,
            "itl_ms": itl_values,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "n_chars": len(text),
        })

        record = {
            "schema_version": 1,
            "task": TASK,
            **item,
            "timestamp": c01.utc_timestamp(),
            "status": "ok" if not trace_errors else "INVALID_TRACE",
            "trace_validation_errors": trace_errors,
            "thermal_status": thermal_status,
            "threads": spec["threads"],
            "threads_batch": spec["threads_batch"],
            "cpu_mask": cpu_list_text(spec["cpu_mask"]),
            "trace_window_graphs": meta.get("trace_window_graphs"),
            "trace_event_count": meta.get("event_count"),
            "trace_dropped_events": meta.get("dropped_events"),
            "instrumented_metrics_role": "diagnostic_sanity_only",
            "prompt_tokens": prompt_tokens,
            **metrics,
            **sched_delta,
            **frequency,
            "total_migrations": sched_delta["migrations"],
            "total_ctx_switches": sched_delta["ctx_switches"],
            "temp_start_c": temp_start,
            "temp_end_c": temp_end,
            "thermal_throttle_before": throttle_before or None,
            "thermal_throttle_after": throttle_after or None,
            "thermal_throttle_delta_detail": throttle,
            "thermal_throttle_delta": throttle_total,
            "t_request_sent_ns": t_sent,
            "t_internal_phase_ns": internal_boundary,
            "t_first_token_ns": token_ts[0],
            "t_last_token_ns": token_ts[-1],
            "machine_state_before": before_run_state,
            "environment_file": environment_file,
            "trace_file": str(trace_path.relative_to(outdir)),
            "token_file": str(token_path.relative_to(outdir)),
            "server_log": str(server_log_path.relative_to(outdir)),
        }
        c01.atomic_json(run_path, record)
        write_runs_csv(outdir)
        return record
    except Exception as exc:
        if not run_path.exists():
            c01.atomic_json(run_path, {
                "schema_version": 1, "task": TASK, **item,
                "timestamp": c01.utc_timestamp(), "status": "FAILED_RUN",
                "error": f"{type(exc).__name__}: {exc}",
                "threads": spec["threads"], "threads_batch": spec["threads_batch"],
                "cpu_mask": cpu_list_text(spec["cpu_mask"]),
                "machine_state_before": before_run_state,
                "environment_file": environment_file,
                "trace_file": str(trace_path.relative_to(outdir)),
                "token_file": str(token_path.relative_to(outdir)),
                "server_log": str(server_log_path.relative_to(outdir)),
            })
            write_runs_csv(outdir)
        raise
    finally:
        if freq is not None:
            freq.stop()
        if proc is not None:
            c01.terminate_process_group(proc)
        log_handle.close()


def write_runs_csv(outdir):
    outdir = Path(outdir)
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((outdir / "raw" / "runs").glob("round_*.json"))
    ]
    path = outdir / "runs.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def environment_record(args, topology, manifest, plan, preflight_state):
    return {
        "schema_version": 1,
        "task": TASK,
        "captured_at": c01.utc_timestamp(),
        "kernel": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "topology_validation": topology,
        "topology": c01.topology_metadata(),
        "preflight_machine_state": preflight_state,
        "power": preflight_state["power"],
        "llama_cpp": {
            "repository_commit": c01.git_head(ROOT / "llama.cpp"),
            "c04_build_manifest": manifest,
            "server_binary": c01.file_identity(args.server_bin, include_hash=True),
            "phase_patch_sha256": sha256_file(PHASE_PATCH),
            "c04_patch_sha256": sha256_file(C04_PATCH),
        },
        "model": args.workload_identities["model"],
        "prompt": args.workload_identities["prompt"],
        "protocol": {
            "rounds": SMOKE_ROUNDS,
            "run_count": 4,
            "order_seed": ORDER_SEED,
            "schedule_design": SCHEDULE_DESIGN,
            "trace_window_graphs": TRACE_WINDOW_GRAPHS,
            "fresh_server_per_run": True,
            "initial_cooldown_s": args.initial_cooldown,
            "cooldown_s": args.cooldown,
            "ctx": args.ctx,
            "batch": args.batch,
            "ubatch": args.ubatch,
            "n_predict": args.n_predict,
            "seed": args.seed,
            "cache_prompt": False,
            "deterministic_generation": True,
        },
        "plan": plan,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the bounded four-request TASK-C04B Intel diagnostic"
    )
    parser.add_argument(
        "--server-bin",
        default=str(ROOT / "llama.cpp" / "build-c04-worker-tail" / "bin" / "llama-server"),
    )
    parser.add_argument(
        "--build-manifest",
        default=str(ROOT / "llama.cpp" / "build-c04-worker-tail" / "c04_build_manifest.json"),
    )
    parser.add_argument(
        "--model", default=str(ROOT / "models" / "Qwen3.5-9B-Q4_K_M.gguf")
    )
    parser.add_argument("--prompt", default=str(HARNESS / "prompt_512.txt"))
    parser.add_argument(
        "--outdir", default=str(ROOT / "results" / "conference_c04" / "worker_tail")
    )
    parser.add_argument("--rounds", type=int, default=SMOKE_ROUNDS)
    parser.add_argument("--order-seed", type=int, default=ORDER_SEED)
    parser.add_argument("--ctx", type=int, default=2048)
    parser.add_argument("--batch", type=int, default=2048)
    parser.add_argument("--ubatch", type=int, default=512)
    parser.add_argument("--n-predict", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--port", type=int, default=8140)
    parser.add_argument("--initial-cooldown", type=int, default=30)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="capture and validate machine/build state without starting a server",
    )
    args = parser.parse_args(argv)
    protect_historical_outputs(args.outdir)
    if args.rounds != SMOKE_ROUNDS:
        parser.error("C04B first checkpoint is frozen at exactly --rounds 2")
    if args.order_seed != ORDER_SEED:
        parser.error(f"C04B --order-seed is frozen at {ORDER_SEED}")
    frozen = {"ctx": 2048, "batch": 2048, "ubatch": 512,
              "n_predict": 256, "seed": 42}
    for name, expected in frozen.items():
        if getattr(args, name) != expected:
            parser.error(f"C04B --{name.replace('_', '-')} is frozen at {expected}")
    if args.initial_cooldown < 30 or args.cooldown < 30:
        parser.error("C04B requires initial and inter-run cooldowns >= 30 seconds")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    for name in ("server_bin", "build_manifest", "model", "prompt"):
        if not Path(getattr(args, name)).exists():
            parser.error(f"--{name.replace('_', '-')} does not exist: {getattr(args, name)}")
    return args


def main(argv=None):
    args = parse_args(argv)
    validate_runtime_environment()
    topology = validate_intel_topology()
    manifest = verify_build_manifest(args.build_manifest, args.server_bin)
    args.workload_identities = verify_workload(args)
    specs = arm_specs()
    preflight_state = machine_state()
    conflicts = interference_warnings(preflight_state["top_cpu_processes"])
    if conflicts:
        print("\n*** C04B PRECHECK WARNING: CPU-HEAVY WORKLOAD DETECTED ***")
        for conflict in conflicts:
            print(f"  {conflict}")
        print("Stop the workload and rerun; no benchmark was started.\n")
        raise SystemExit(2)

    if args.preflight_only:
        print(json.dumps({
            "status": "PASS",
            "task": TASK,
            "topology": topology,
            "machine_state": preflight_state,
            "binary_sha256": manifest["binary"]["sha256"],
            "workload": args.workload_identities,
            "would_run": build_schedule(),
        }, indent=2))
        return 0

    plan = ensure_plan(args.outdir, specs)
    environment = environment_record(
        args, topology, manifest, plan, preflight_state
    )
    environment_file = register_environment(args.outdir, environment)

    print(f"[C04B] initial cooldown {args.initial_cooldown}s", flush=True)
    time.sleep(args.initial_cooldown)
    completed = 0
    for item in plan["schedule"]:
        if completed:
            print(f"[C04B] cooldown {args.cooldown}s", flush=True)
            time.sleep(args.cooldown)
        print(
            f"[C04B] round={item['round']} seq={item['sequence_index']} "
            f"arm={item['arm']}", flush=True,
        )
        record = run_arm(
            args, item, specs[item["arm"]], environment_file
        )
        completed += 1
        print(
            f"  status={record['status']} ttft={record['ttft_ms']:.3f}ms "
            f"itl_p95={record['itl_p95_ms']:.3f}ms "
            f"events={record['trace_event_count']} "
            f"thermal={record['thermal_status']}", flush=True,
        )
        if record["status"] != "ok":
            raise RuntimeError(
                "trace failed closed; raw run was preserved and pilot stopped"
            )

    runs_csv = write_runs_csv(args.outdir)
    print(f"[C04B] completed exactly {completed} runs -> {runs_csv}")
    print(
        "Analyze with: python3 conference/analysis/c04_analyze.py "
        f"--input {args.outdir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
