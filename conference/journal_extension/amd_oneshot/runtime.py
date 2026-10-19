"""Prepared real-request backend. Importing this module never runs inference."""
import http.client
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "conference" / "experiments"))
sys.path.insert(0, str(ROOT / "harness"))
import c02_external_oracle as c02
from .calibration import DetectorState
from conference.tools.c03_amd_preflight import collect_thermal_state

bl, ro, tr = c02.bl, c02.ro, c02.tr


class RuntimeFailure(RuntimeError):
    def __init__(self, reason, *, retryable=False, tokens_seen=0):
        super().__init__(reason)
        self.reason, self.retryable, self.tokens_seen = reason, retryable, tokens_seen


def _write(path, value):
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)


def cpu_class(cpu, groups):
    """Classification is supplied by hardware inventory, never CPU-number lore."""
    return next((name for name, members in groups.items() if cpu in members), "unclassified")


class _Samples(list):
    def __init__(self, monitor):
        super().__init__()
        self.monitor = monitor

    def append(self, sample):
        extra_start = time.perf_counter_ns()
        m = self.monitor
        sample["tasks"] = tr.sample_tasks(m.pid)
        if m._frequency_time is None or sample["t_ns"] - m._frequency_time >= 1_000_000_000:
            m._frequency, m._frequency_time = frequency_snapshot(), sample["t_ns"]
        sample["frequency_mhz"] = m._frequency
        sample["frequency_snapshot_t_ns"] = m._frequency_time
        # C02's acquisition is reused; normalize with this kernel's USER_HZ.
        if self and sample["cputime_jiffies"] is not None and self[-1]["cputime_jiffies"] is not None:
            delta = sample["cputime_jiffies"] - self[-1]["cputime_jiffies"]
            sample["norm_ctx_per_cpu_s"] = ((sample["ctx_switches"] - self[-1]["ctx_switches"]) * os.sysconf("SC_CLK_TCK") / delta if delta > 0 else 0.)
        if m.decision_start_ns is not None and sample["t_ns"] >= m.decision_start_ns:
            m.check_safety()
            # The detector receives exactly these two fields: no markers.
            event = m.detector.feed({key: sample[key] for key in ("t_ns", "norm_ctx_per_cpu_s")})
            if event:
                m.decisions.append(event)
                m.flip_count += 1
                if event["state"] == "decode" and m.external_detect_ns is None:
                    m.external_detect_ns = event["t_ns"]
                    if m.route:
                        m.route(event["t_ns"], "external_proc")
            sample["state"] = m.detector.state
        sample["extra_observation_us"] = (time.perf_counter_ns() - extra_start) / 1000.
        sample["total_observation_us"] = sample.get("read_cost_ns", 0) / 1000. + sample["extra_observation_us"]
        super().append(sample)


class RequestMonitor(c02.ProcPhaseMonitor):
    def __init__(self, pid, interval_s, actuator, model, route=None, safety_check=None, abort=None):
        # Disable C02 threshold decisions; retain its /proc acquisition loop.
        super().__init__(pid, interval_s, float("inf"), -float("inf"), 2, actuator)
        self.model, self.route = model, route
        self.detector, self.decisions = DetectorState(model), []
        self.samples = _Samples(self)
        self.error = None
        self.safety_check, self.abort = safety_check, abort
        self.safety_samples, self.abort_reasons = [], []
        self.abort_runtime = False
        self._last_safety = None
        self._frequency, self._frequency_time = {}, None
        self.thread_cpu_ns = None

    def check_safety(self):
        now = time.monotonic()
        if self._last_safety is not None and now - self._last_safety < 1:
            return
        self._last_safety = now
        try:
            result = (self.safety_check or live_safety_snapshot)()
            reasons = list(result.get("reasons", []))
            if result.get("temp_c") is None:
                reasons.append("temperature_unreadable")
            elif result["temp_c"] >= 95:
                reasons.append("critical_temperature")
            if result.get("ac_online") is not True:
                reasons.append("ac_disconnected_or_unknown")
        except Exception as exc:
            result, reasons = {"error": str(exc)}, ["safety_check_failed"]
        self.safety_samples.append({"t_ns": bl.now_ns(), **result})
        if reasons and not self.abort_runtime:
            self.abort_runtime = True
            self.abort_reasons.extend(sorted(set(reasons)))
            if self.abort:
                self.abort()

    def run(self):
        cpu_start = time.thread_time_ns()
        try:
            super().run()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.thread_cpu_ns = time.thread_time_ns() - cpu_start

    def arm_for_request(self):
        self.detector, self.decisions = DetectorState(self.model), []
        return super().arm_for_request()


def frequency_snapshot():
    return {int(path.parent.parent.name[3:]): value / 1000.
            for path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")
            if (value := bl._read_int(path)) is not None}


def package_temperature():
    # bench_lib.package_temp_c is Intel-coretemp-specific. Reuse the validated
    # C03 portable thermal selector (including AMD k10temp Tctl/Tdie evidence).
    return collect_thermal_state().get('selected_c')


def affinity_snapshot(pid):
    """Read-only eligibility masks, including helpers; never sets affinity."""
    main = sorted(os.sched_getaffinity(pid))
    threads = {}
    for path in Path(f"/proc/{pid}/task").glob("[0-9]*"):
        try:
            threads[path.name] = sorted(os.sched_getaffinity(int(path.name)))
        except (OSError, ValueError):
            continue
    return {"main_thread": main, "per_thread": threads}


def live_safety_snapshot():
    supplies = []
    for path in Path("/sys/class/power_supply").glob("*"):
        try:
            if (path / "type").read_text().strip() in ("Mains", "USB", "USB_C"):
                supplies.append((path / "online").read_text().strip() == "1")
        except OSError:
            continue
    return {"temp_c": package_temperature(), "ac_online": any(supplies) if supplies else None}


def reserve_port(requested=None):
    """Hold an exclusively bound localhost socket until the child is launched."""
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        reservation.bind((ro.HOST, requested or 0))
        reservation.listen(1)
        return reservation, reservation.getsockname()[1]
    except BaseException:
        reservation.close()
        raise


def validate_trace(samples, sent, last, boundary):
    times = [sample["t_ns"] for sample in samples]
    valid = [s for s in samples if s.get("tasks")
             and isinstance(s.get("norm_ctx_per_cpu_s"), (int, float))
             and math.isfinite(s["norm_ctx_per_cpu_s"]) and s["norm_ctx_per_cpu_s"] >= 0]
    reasons = []
    if len(times) < 2 or any(b <= a for a, b in zip(times, times[1:])):
        reasons.append("nonmonotonic_or_missing_samples")
    if not times or times[0] > sent + 100_000_000 or times[-1] < last - 100_000_000:
        reasons.append("start_or_end_not_covered")
    if sum(s["t_ns"] < boundary - 200_000_000 for s in valid) < 3:
        reasons.append("insufficient_prefill_samples")
    if sum(s["t_ns"] > boundary + 200_000_000 for s in valid) < 3:
        reasons.append("insufficient_decode_samples")
    gaps = [{"start_ns": a, "end_ns": b, "gap_ms": (b-a)/1e6}
            for a, b in zip(times, times[1:]) if b-a > 50_000_000]
    return {"valid": not reasons, "reasons": reasons, "gaps_over_50ms": gaps}


def residency_summary(samples):
    """Sampled last-CPU observations; CPU ticks exclude idle stale CPU fields.

    Tick deltas cannot locate intervening migrations, so attribution to the
    ending CPU is explicitly an estimate rather than continuous residency.
    """
    result, previous = {}, {}
    for sample in samples:
        for tid, task in sample["tasks"].items():
            entry = result.setdefault(str(tid), {"cpu_observations": {},
                "active_tick_estimates_by_cpu": {}, "observed_cpu_changes": 0})
            cpu = str(task["cpu"])
            entry["cpu_observations"][cpu] = entry["cpu_observations"].get(cpu, 0) + 1
            if tid in previous:
                old = previous[tid]
                entry["observed_cpu_changes"] += int(old["cpu"] != task["cpu"])
                delta = max(0, task["cpu_ticks"] - old["cpu_ticks"])
                entry["active_tick_estimates_by_cpu"][cpu] = entry["active_tick_estimates_by_cpu"].get(cpu, 0) + delta
            previous[tid] = task
    return result


def server_command(config, spec, port):
    command = []
    if spec["cpus"] is not None:
        command += ["taskset", "-c", ",".join(map(str, spec["cpus"]))]
    command += [str(config["server_bin"]), "-m", str(config["model"]), "-ngl", "0",
                "-t", str(spec["threads"]), "-tb", str(spec["threads_batch"]),
                "-c", str(config.get("ctx", 2048)), "-b", str(config.get("batch", 2048)),
                "-ub", str(config.get("ubatch", 512)), "-np", "1", "--host", ro.HOST,
                "--port", str(port)]
    return command


def _stream(port, payload, timeout_s, tokens):
    """run_once SSE format, with an overall deadline and retained partial tokens."""
    start = bl.now_ns()
    deadline = time.monotonic() + timeout_s
    conn = None
    pieces, final, buffer = [], None, b""
    try:
        conn = ro.post_json(port, "/completion", payload, timeout_s=timeout_s)
        sock = conn.sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeFailure(f"http_status_{response.status}", tokens_seen=len(tokens))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline")
            sock.settimeout(remaining)
            chunk = response.read1(4096)
            if not chunk:
                break
            timestamp = bl.now_ns()
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip().startswith(b"data:"):
                    continue
                raw = line.strip()[5:].strip()
                if not raw or raw == b"[DONE]":
                    continue
                obj = json.loads(raw)
                if obj.get("content"):
                    tokens.append(timestamp)
                    pieces.append(obj["content"])
                if obj.get("stop"):
                    final = obj
        return start, "".join(pieces), final
    except (OSError, http.client.HTTPException) as exc:
        # Timeouts are incomplete requests, not safe transient connection retries.
        retry = not tokens and not isinstance(exc, TimeoutError)
        raise RuntimeFailure("connection_before_tokens" if retry else "incomplete_request",
                             retryable=retry, tokens_seen=len(tokens)) from exc
    finally:
        if conn is not None:
            conn.close()


def run_request(config, item, arm_spec, run_dir, detector_model):
    """One fresh owned server; caller owns scheduling, ledgers and retry policy."""
    spec = dict(arm_spec)
    source = spec["phase_source"]
    if source not in ("none", "external_proc", "internal_oracle"):
        raise ValueError("invalid phase_source")
    if item["arm"] == "STOCK" and spec["cpus"] is not None:
        raise ValueError("STOCK must be unpinned")
    if source != "none" and (spec["threads"], spec["threads_batch"]) != (4, 4):
        raise ValueError("dynamic arms require the frozen 4/4 budget")
    if source != "none" and (not spec["cpus"] or not spec["target_cpus"]):
        raise ValueError("dynamic masks required")
    if config.get("interval_ms", 20) != 20:
        raise ValueError("observer interval must remain 20 ms")
    DetectorState(detector_model)  # Validate before creating artifacts or processes.
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "server.log"
    tokens, warmup_tokens, proc, monitor, watcher = [], [], None, None, None
    request_start, offline_markers = None, []
    timings, record = {}, {**item, "status": "failed", "phase_source": source,
                          "arm_spec": spec, "threads": spec["threads"],
                          "threads_batch": spec["threads_batch"],
                          "planned_initial_cpus": spec['cpus'], 'planned_final_cpus': spec.get('target_cpus'),
                          'initial_cpus': None, 'final_cpus': None}
    reservation = None
    started = time.monotonic()
    try:
        with log_path.open("xb") as log:
            reservation, port = reserve_port(config.get("port"))
            reservation.close()
            reservation = None
            command = server_command(config, spec, port)
            record.update(server_command=command, server_port=port)
            proc = subprocess.Popen(command, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                ro.wait_for_health(port, proc, timeout_s=config.get("startup_timeout_s", 120))
            except RuntimeError as exc:
                raise RuntimeFailure("startup_timeout" if proc.poll() is None else "server_exited",
                                     retryable=proc.poll() is None) from exc
            timings["startup_s"] = time.monotonic() - started
            timings["startup_readiness_s"] = timings["startup_s"]
            prompt = Path(config["prompt"]).read_text(encoding="utf-8")
            warm_start = time.monotonic()
            warm_sent, warm_text, warm_final = _stream(port, {"prompt": "Warmup.", "n_predict": 1, "temperature": 0,
                           "stream": True, "cache_prompt": False}, config.get("request_timeout_s", 180), warmup_tokens)
            record['warmup'] = {'t_sent_ns': warm_sent, 'text': warm_text, 'final': warm_final,
                                'excluded_from_performance': True}
            timings["warmup_s"] = time.monotonic() - warm_start
            time.sleep(config.get("warmup_settle_s", 1.0))
            timings["warmup_with_settle_s"] = time.monotonic() - warm_start
            log.flush()
            offset = log_path.stat().st_size
            record["affinity_start"] = affinity_snapshot(proc.pid)
            if spec["cpus"] is not None and record["affinity_start"]["main_thread"] != sorted(spec["cpus"]):
                raise RuntimeFailure("initial_server_affinity_mismatch")
            actuator = c02.AffinityActuator(proc.pid, spec.get("target_cpus") or [])
            monitor = RequestMonitor(proc.pid, .020, actuator, detector_model,
                                     actuator.apply if source == "external_proc" else None,
                                     safety_check=config.get("live_safety_check"), abort=proc.terminate)
            watcher = c02.PhaseMarkWatcher(log_path, offset,
                                          actuator.apply if source == "internal_oracle" else None)
            monitor.start()
            watcher.start()
            time.sleep(config.get("monitor_preroll_s", .3))
            before, temp_start = bl.sched_snapshot(proc.pid), package_temperature()
            throttle_before = c02.thermal_throttle_snapshot()
            record.update(temp_start_c=temp_start, thermal_throttle_before=throttle_before)
            frequency_start = frequency_snapshot()
            armed = monitor.arm_for_request()
            request_start = time.monotonic()
            sent, text, final = _stream(port, {"prompt": prompt,
                "n_predict": config.get("n_predict", 256), "seed": config.get("seed", 42),
                "temperature": 0, "stream": True, "ignore_eos": True, "cache_prompt": False},
                config.get("request_timeout_s", 180), tokens)
            timings["request_elapsed_s"] = time.monotonic() - request_start
            after, temp_end = bl.sched_snapshot(proc.pid), package_temperature()
            throttle_after = c02.thermal_throttle_snapshot()
            record.update(temp_end_c=temp_end, thermal_throttle_after=throttle_after,
                          thermal_throttle_delta=c02.thermal_throttle_delta(throttle_before, throttle_after))
            record["affinity_end"] = affinity_snapshot(proc.pid)
            monitor.stop_flag.set()
            monitor.join(timeout=2)
            watcher.stop_flag.set()
            watcher.join(timeout=2)
            log.flush()
            markers = c02.parse_phase_markers_offline(log_path, offset)
            offline_markers = markers
            boundary = c02.first_internal_boundary(markers, sent)
            if monitor.abort_runtime:
                raise RuntimeFailure("critical_safety_stop", tokens_seen=len(tokens))
            if final is None or len(tokens) != config.get("n_predict", 256):
                raise RuntimeFailure("incomplete_request", tokens_seen=len(tokens))
            if boundary is None or watcher.t_internal_phase_ns != boundary or watcher.t_marker_seen_ns is None:
                raise RuntimeFailure("missing_live_marker", tokens_seen=len(tokens))
            samples = [s for s in monitor.samples if s["t_ns"] >= armed]
            trace_validation = validate_trace(samples, sent, tokens[-1], boundary)
            record["trace_validation"] = trace_validation
            if (monitor.error or monitor.is_alive() or watcher.is_alive() or not trace_validation["valid"]):
                raise RuntimeFailure("incomplete_cpu_trace", tokens_seen=len(tokens))
            # Missing external criterion crossings are valid policy outcomes,
            # never a reason to replace a fresh held-out performance request.
            missing_required_action = (source == 'internal_oracle' or
                (source == 'external_proc' and monitor.external_detect_ns is not None)) and actuator.record is None
            if missing_required_action or (actuator.record is not None and (
                    not actuator.record['n_tids_succeeded'] or actuator.record['n_tids_failed'])):
                raise RuntimeFailure("missing_or_failed_actuation", tokens_seen=len(tokens))
            if source == "internal_oracle" and actuator.record["t_trigger_ns"] != boundary:
                raise RuntimeFailure("wrong_oracle_boundary", tokens_seen=len(tokens))
            metrics = c02.token_metrics(sent, tokens)
            record.update(status="ok", **metrics, **bl.sched_delta(before, after),
                          temp_start_c=temp_start, temp_end_c=temp_end,
                          frequency_start_mhz=frequency_start, frequency_end_mhz=frequency_snapshot(),
                          thermal_throttle_before=throttle_before, thermal_throttle_after=throttle_after,
                          thermal_throttle_delta=c02.thermal_throttle_delta(throttle_before, throttle_after),
                          t_request_sent_ns=sent, t_internal_phase_ns=boundary,
                          t_marker_seen_ns=watcher.t_marker_seen_ns, actuation=actuator.record,
                          text=text, final=final,
                          initial_cpus=record['affinity_start']['main_thread'],
                          final_cpus=record['affinity_end']['main_thread'], target_cpus=spec.get('target_cpus'),
                          policy_action_status=('NO_EXTERNAL_TRIGGER' if source == 'external_proc' and actuator.record is None
                                                else 'APPLIED' if actuator.record is not None else 'NOT_APPLICABLE'),
                          samples=samples, markers=markers,
                          per_tid_cpu_observations=residency_summary(samples),
                          model_load_timing_source="health_readiness_upper_bound")
            record["throttle_warning"] = bool((record["thermal_throttle_delta"] or {}).get("total", 0) > 0)
    except BaseException as exc:
        record.update(error_type=type(exc).__name__, error=str(exc), tokens_seen=len(tokens))
        if monitor is not None and monitor.abort_runtime and isinstance(exc, Exception):
            record.update(error="critical_safety_stop", error_type="RuntimeFailure")
            raise RuntimeFailure("critical_safety_stop", tokens_seen=len(tokens)) from exc
        if isinstance(exc, Exception) and not isinstance(exc, RuntimeFailure):
            raise RuntimeFailure(f"{type(exc).__name__}: {exc}", tokens_seen=len(tokens)) from exc
        raise
    finally:
        if reservation is not None:
            reservation.close()
        timings["backend_elapsed_s"] = time.monotonic() - started
        if request_start is not None and "request_elapsed_s" not in timings:
            timings["request_elapsed_s"] = time.monotonic() - request_start
        for observer in (monitor, watcher):
            if observer is not None:
                observer.stop_flag.set()
                observer.join(timeout=2)
        if request_start is not None:
            # Preserve telemetry on partial requests and safety aborts as well.
            try:
                record.setdefault("temp_end_c", package_temperature())
                record.setdefault("thermal_throttle_after", c02.thermal_throttle_snapshot())
                record["thermal_throttle_delta"] = c02.thermal_throttle_delta(
                    record.get("thermal_throttle_before", {}), record["thermal_throttle_after"])
            except Exception as exc:
                record["end_thermal_read_error"] = str(exc)
            try:
                record.setdefault("affinity_end", affinity_snapshot(proc.pid))
            except OSError:
                record.setdefault("affinity_end", None)
        if proc is not None:
            c02.c01.terminate_process_group(proc)
        record['server_returncode'] = proc.poll() if proc is not None else None
        record['owned_server_cleanup_success'] = proc is None or proc.poll() is not None
        record['observers_joined'] = {name: observer is None or not observer.is_alive()
                                     for name, observer in [('monitor', monitor), ('marker_watcher', watcher)]}
        timings["backend_elapsed_s"] = time.monotonic() - started
        action = monitor.actuator.record if monitor else None
        external = monitor.external_detect_ns if monitor else None
        boundary = watcher.t_internal_phase_ns if watcher else None
        delivered = watcher.t_marker_seen_ns if watcher else None
        record.update(t_external_detect_ns=external,
                      t_affinity_start_ns=action.get('t_affinity_start_ns') if action else None,
                      t_affinity_done_ns=action.get('t_affinity_done_ns') if action else None,
                      action_start_offset_ms=(action['t_affinity_start_ns']-boundary)/1e6 if action and boundary is not None else None,
                      action_done_offset_ms=(action['t_affinity_done_ns']-boundary)/1e6 if action and boundary is not None else None,
                      marker_delivery_ms=(delivered-boundary)/1e6 if delivered is not None and boundary is not None else None,
                      detect_offset_ms=(external-boundary)/1e6 if external is not None and boundary is not None else None,
                      action_offset_ms=(action["t_affinity_done_ns"]-boundary)/1e6 if action and boundary is not None else None,
                      affinity_cost_us=action.get("affinity_cost_us") if action else None)
        record.update(timings=timings, server_log=str(log_path), token_file=str(run_dir / "tokens.json"),
                      phase_log=str(run_dir / "phase.json"),
                      startup_elapsed_s=timings.get("startup_s"), warmup_elapsed_s=timings.get("warmup_with_settle_s"),
                      request_elapsed_s=timings.get("request_elapsed_s"),
                      backend_total_elapsed_s=timings["backend_elapsed_s"],
                      thermal_warning=bool((record.get("thermal_throttle_delta") or {}).get("total", 0) > 0),
                      abort_runtime=monitor.abort_runtime if monitor else False,
                      abort_reasons=monitor.abort_reasons if monitor else [],
                      safety_samples=monitor.safety_samples if monitor else [])
        _write(run_dir / "tokens.json", {"token_ts_ns": tokens})
        _write(run_dir / 'warmup.json', {'token_ts_ns': warmup_tokens,
                                       'warmup': record.get('warmup'), 'excluded_from_performance': True})
        _write(run_dir / "phase.json", {"proc_samples": list(monitor.samples) if monitor else [],
               "detector_decisions": monitor.decisions if monitor else [],
               "markers_live": watcher.markers if watcher else [],
               "markers_offline_cross_check": offline_markers,
               "monitor_error": monitor.error if monitor else None,
               "monitor_thread_cpu_ns": monitor.thread_cpu_ns if monitor else None,
               "actuation": monitor.actuator.record if monitor else None})
        cleanup_failed = record['status'] == 'ok' and (not record['owned_server_cleanup_success'] or not all(record['observers_joined'].values()))
        if cleanup_failed:
            record.update(status='failed', error='owned_runtime_cleanup_failed', error_type='RuntimeFailure')
        _write(run_dir / "record.json", record)
        if cleanup_failed:
            raise RuntimeFailure('owned_runtime_cleanup_failed', tokens_seen=len(tokens))
    return record
