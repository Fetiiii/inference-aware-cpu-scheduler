import json
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conference" / "experiments"))
import c04_worker_tail as c04  # noqa: E402


@pytest.fixture
def mock_ps(monkeypatch):
    def configure(stdout, returncode=0, stderr=""):
        process = MagicMock()
        process.pid = 12346
        process.args = ["ps", "-eo", "pid=,comm=,%cpu=", "--sort=-%cpu"]
        process.returncode = returncode
        process.poll.return_value = returncode
        process.communicate.return_value = (stdout, stderr)
        process.__enter__.return_value = process
        popen = MagicMock(return_value=process)
        monkeypatch.setattr(c04.subprocess, "Popen", popen)
        monkeypatch.setattr(c04.os, "getpid", lambda: 12345)
        return popen
    return configure


def test_process_sampler_excludes_only_own_and_sampler_pids_before_limit(mock_ps):
    popen = mock_ps("12346 ps 100.0\n12345 python3 90.0\n54321 firefox 2.0\n")
    rows = c04.top_cpu_processes(limit=1)
    assert rows == [{"pid": 54321, "comm": "firefox", "cpu_pct": 2.0}]
    assert c04.interference_warnings(rows) == []
    popen.assert_called_once_with(
        ["ps", "-eo", "pid=,comm=,%cpu=", "--sort=-%cpu"],
        stdout=c04.subprocess.PIPE, stderr=c04.subprocess.PIPE, text=True,
    )


@pytest.mark.parametrize("command,cpu_pct", [
    ("ps", 100.0), ("gcc", 10.0), ("llama-server", 10.0),
    ("codex", 10.0), ("unknown-heavy", 75.0),
])
def test_sampler_retains_real_cpu_interference(mock_ps, command, cpu_pct):
    mock_ps(f"12346 ps 100.0\n54321 {command} {cpu_pct}\n")
    rows = c04.top_cpu_processes()
    assert rows == [{"pid": 54321, "comm": command, "cpu_pct": cpu_pct}]
    warnings = c04.interference_warnings(rows)
    assert len(warnings) == 1
    assert f"pid=54321 comm={command}" in warnings[0]


@pytest.mark.parametrize("command,cpu_pct", [("gcc", 9.9), ("unknown", 74.9)])
def test_interference_thresholds_unchanged(mock_ps, command, cpu_pct):
    mock_ps(f"54321 {command} {cpu_pct}\n")
    assert c04.interference_warnings(c04.top_cpu_processes()) == []


def test_sampler_command_failure_is_not_silently_ignored(mock_ps):
    mock_ps("partial output\n", returncode=1, stderr="ps failed\n")
    with pytest.raises(c04.subprocess.CalledProcessError) as exc:
        c04.top_cpu_processes()
    assert exc.value.returncode == 1
    assert exc.value.output == "partial output\n"
    assert exc.value.stderr == "ps failed\n"


def event(**overrides):
    base = {"cpu_start": 0, "cpu_arrival": 0, "migrated_start_to_arrival": 0}
    base.update(overrides)
    return base


def test_trace_disabled_has_no_environment_or_arm_output(tmp_path):
    assert c04.trace_environment({"BASE": "1", "C04_WORKER_TAIL_ARM_FILE": "stale"}, enabled=False) == {"BASE": "1"}
    arm = tmp_path / "trace.arm"
    assert c04.create_trace_arm_file(arm, enabled=False) is False
    assert not arm.exists()


def test_frozen_arms_and_schedule():
    specs = c04.arm_specs()
    assert specs["P8_ONLY"] == {"threads": 8, "threads_batch": 8, "cpu_mask": [0, 2, 4, 6, 8, 10, 12, 14]}
    assert specs["PE_ALLOWED_T8_TB16"] == {"threads": 8, "threads_batch": 16, "cpu_mask": [0, 2, 4, 6, 8, 10, 12, 14, 16, 17, 18, 19, 20, 21, 22, 23]}
    schedule = c04.build_schedule()
    assert len(schedule) == 4
    for round_number in (1, 2):
        assert {row["arm"] for row in schedule if row["round"] == round_number} == set(c04.ARMS)
    assert [row["arm"] for row in schedule] == [
        "P8_ONLY", "PE_ALLOWED_T8_TB16", "PE_ALLOWED_T8_TB16", "P8_ONLY"]
    assert [row["global_sequence_index"] for row in schedule] == [1, 2, 3, 4]
    assert all(row["randomized_order_seed"] == 4404 for row in schedule)
    assert all(row["schedule_design"] == "counterbalanced_two_rounds_v1" for row in schedule)
    assert [row["round_order_source"] for row in schedule] == [
        "seeded_shuffle", "seeded_shuffle", "reverse_round_1", "reverse_round_1"]
    for arm in c04.ARMS:
        assert sorted(row["sequence_index"] for row in schedule if row["arm"] == arm) == [1, 2]
    assert schedule == c04.build_schedule()
    with pytest.raises(ValueError):
        c04.build_schedule(rounds=6)


def test_counterbalanced_plan_records_design_and_rejects_previous_protocol(tmp_path):
    plan = c04.ensure_plan(tmp_path, c04.arm_specs())
    assert plan["schedule_design"] == "counterbalanced_two_rounds_v1"
    assert plan["schedule"] == c04.build_schedule()
    old_plan = {**plan, "schedule_design": "independent_per_round_shuffle"}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(old_plan))
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="incompatible"):
        c04.ensure_plan(tmp_path, c04.arm_specs())
    assert path.read_bytes() == before


def test_actual_cpu_class_and_migration():
    assert c04.classify_worker_event(event(cpu_start=2, cpu_arrival=2)) == "stable_P"
    assert c04.classify_worker_event(event(cpu_start=17, cpu_arrival=17)) == "stable_E"
    assert c04.classify_worker_event(event(cpu_start=2, cpu_arrival=17)) == "migrated"


def test_trace_limit_and_overflow_fail_closed(tmp_path):
    trace = tmp_path / "trace.jsonl"
    meta = {"record_type": "meta", "trace_window_graphs": 16, "buffer_capacity": 131072, "event_count": 1, "dropped_events": 1}
    sample = {"record_type": "event", "epoch": 1}
    trace.write_text(json.dumps(meta) + "\n" + json.dumps(sample) + "\n", encoding="utf-8")
    loaded_meta, events = c04.read_trace(trace)
    errors = c04.trace_validation_errors(loaded_meta, events)
    assert any("dropped 1" in value for value in errors)
    assert any("every frozen decode epoch" in value for value in errors)

    valid_meta = {**meta, "event_count": 128, "dropped_events": 0}
    valid_events = trace_events()
    assert c04.trace_validation_errors(valid_meta, valid_events) == []


def trace_events():
    return [{"record_type": "event", "epoch": epoch, "node_index": 1,
             "worker_index": worker, "active_workers": 8,
             "t_start_ns": epoch * 100000 + 1,
             "t_arrival_ns": epoch * 100000 + 1000 + worker,
             "t_exit_ns": epoch * 100000 + 2000,
             "cpu_start": worker * 2, "cpu_arrival": worker * 2, "cpu_exit": worker * 2,
             "op": "MUL_MAT", "src0_type": "q4_K", "is_q4_k": True,
             "migrated_start_to_arrival": False, "ne": [256, 1, 1, 1],
             "src0_ne": [256, 256, 1, 1], "src1_ne": [256, 1, 1, 1]}
            for epoch in range(1, 17) for worker in range(8)]


def test_missing_and_duplicate_workers_and_unordered_timestamps_invalid():
    events = trace_events()
    meta = {"trace_window_graphs": 16, "buffer_capacity": 131072,
            "event_count": len(events) - 1, "dropped_events": 0}
    assert any("eight decode workers" in s for s in c04.trace_validation_errors(meta, events[1:]))
    events[0]["worker_index"] = 1
    meta["event_count"] = len(events)
    assert any("exactly once" in s for s in c04.trace_validation_errors(meta, events))
    events[0]["t_exit_ns"] = 0
    assert any("unordered" in s for s in c04.trace_validation_errors(meta, events))


def test_patch_gates_decode_window_at_phase_boundary_and_mul_mat_only():
    text = c04.C04_PATCH.read_text(encoding="utf-8")
    assert "c04_trace_armed && !batched" in text
    assert "c04_trace_window_graphs = 16" in text
    assert ".active = false" in text
    assert "access(c04_arm_path, F_OK) == 0" in text
    assert "node->op == GGML_OP_MUL_MAT" in text
    assert "stored_event->t_arrival_ns = c04_worker_tail_now_ns()" in text
    hot_path = text[text.index("struct c04_worker_tail_event * stored_event = NULL"):text.index("--- a/src/llama-context.cpp")]
    assert "fprintf" not in hot_path and "fopen" not in hot_path
    arrival = hot_path.index("stored_event->t_arrival_ns = c04_worker_tail_now_ns()")
    barrier = hot_path.index("ggml_barrier(state->threadpool)")
    assert arrival < barrier
    assert "c04_worker_tail_reserve" not in hot_path[arrival:barrier]
    assert hot_path.index("stored_event->t_exit_ns = c04_worker_tail_now_ns()") > barrier


def test_preflight_never_starts_server(monkeypatch, tmp_path):
    args = SimpleNamespace(preflight_only=True, server_bin="unused", build_manifest="unused")
    monkeypatch.setattr(c04, "parse_args", lambda argv: args)
    monkeypatch.setattr(c04, "validate_intel_topology", lambda: {})
    monkeypatch.setattr(c04, "verify_build_manifest", lambda *a: {"binary": {"sha256": "mock"}})
    monkeypatch.setattr(c04, "verify_workload", lambda *a: {})
    monkeypatch.setattr(c04, "machine_state", lambda: {"top_cpu_processes": []})
    monkeypatch.setattr(c04.subprocess, "Popen", lambda *a, **k: pytest.fail("preflight started a process"))
    assert c04.main([]) == 0
    assert not list(tmp_path.iterdir())


def test_two_round_argument_gate_happens_before_any_run():
    with pytest.raises(SystemExit) as exc:
        c04.parse_args(["--rounds", "6"])
    assert exc.value.code == 2


def test_plan_refuses_overwrite_or_resume(tmp_path):
    c04.ensure_plan(tmp_path, c04.arm_specs())
    run_dir = tmp_path / "raw" / "runs"
    run_dir.mkdir(parents=True)
    raw = run_dir / "round_01.json"
    raw.write_text("preserved", encoding="utf-8")
    with pytest.raises(RuntimeError, match="overwrite or resume"):
        c04.ensure_plan(tmp_path, c04.arm_specs())
    assert raw.read_text() == "preserved"


def test_manifest_checks_shared_libraries_and_native_configuration(tmp_path):
    server, library = tmp_path / "server", tmp_path / "libllama.so"
    server.write_bytes(b"fake-server")
    library.write_bytes(b"fake-library")
    manifest = {
        "source_commit": c04.PINNED_LLAMA_COMMIT,
        "patch_sha256": {"phase_mark": c04.sha256_file(c04.PHASE_PATCH),
                         "c04_worker_tail": c04.sha256_file(c04.C04_PATCH)},
        "binary": {"path": str(server), "sha256": c04.sha256_file(server),
                   "embedded_source_commit": c04.PINNED_LLAMA_COMMIT},
        "shared_libraries": [{"path": str(library), "sha256": c04.sha256_file(library)}],
        "cmake_cache": {"CMAKE_BUILD_TYPE": "Release", "BUILD_SHARED_LIBS": "ON",
                        "GGML_NATIVE": "ON", "GGML_OPENMP": "ON",
                        "GGML_CPU_REPACK": "ON", "GGML_LLAMAFILE": "ON",
                        "LLAMA_BUILD_UI": "OFF", "LLAMA_USE_PREBUILT_UI": "OFF"},
        "capabilities": ["PHASE_MARK", "C04_WORKER_TAIL"],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert c04.verify_build_manifest(path, server)["source_commit"] == c04.PINNED_LLAMA_COMMIT
    for key in ("LLAMA_BUILD_UI", "LLAMA_USE_PREBUILT_UI"):
        manifest["cmake_cache"][key] = "ON"
        path.write_text(json.dumps(manifest))
        with pytest.raises(RuntimeError, match="CMake configuration"):
            c04.verify_build_manifest(path, server)
        manifest["cmake_cache"][key] = "OFF"
    manifest["binary"]["embedded_source_commit"] = "70f3bc1"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="embedded llama.cpp"):
        c04.verify_build_manifest(path, server)
    manifest["binary"]["embedded_source_commit"] = c04.PINNED_LLAMA_COMMIT
    path.write_text(json.dumps(manifest))
    library.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="shared-library"):
        c04.verify_build_manifest(path, server)


def test_build_helper_and_runner_do_not_change_system_settings():
    script = (ROOT / "conference" / "diagnostic" / "build_c04_worker_tail.sh").read_text()
    assert "GGML_NATIVE=ON" in script
    assert "GGML_CPU_REPACK=ON" in script
    assert "GGML_OPENMP=ON" in script
    assert "-DLLAMA_BUILD_UI=OFF" in script
    assert "-DLLAMA_USE_PREBUILT_UI=OFF" in script
    for forbidden in ("sudo ", "sysctl -w", "cpupower frequency-set", "powerprofilesctl set"):
        assert forbidden not in script


@pytest.mark.parametrize("directory", ["conference_c01", "conference_c02", "conference_c03", "conference_c04/recon"])
def test_historical_artifacts_are_rejected_before_any_write(directory):
    with pytest.raises(RuntimeError):
        c04.ensure_plan(ROOT / "results" / directory, c04.arm_specs())


def test_openmp_overrides_cannot_silently_change_worker_placement(monkeypatch):
    monkeypatch.setenv("OMP_PROC_BIND", "true")
    assert c04.runtime_environment()["OMP_PROC_BIND"] == "true"
    with pytest.raises(RuntimeError, match="OMP_PROC_BIND"):
        c04.validate_runtime_environment()


def test_workload_hash_cannot_silently_select_second_model(monkeypatch):
    args = SimpleNamespace(model="fake", prompt="fake")
    monkeypatch.setattr(c04.c01, "file_identity", lambda *a, **k: {"sha256": "wrong"})
    with pytest.raises(RuntimeError, match="frozen Intel"):
        c04.verify_workload(args)


def test_output_is_c04_only_and_arm_file_is_exclusive(tmp_path):
    assert "conference_c04/worker_tail" in str(Path("results/conference_c04/worker_tail"))
    arm = tmp_path / "one.arm"
    assert c04.create_trace_arm_file(arm)
    with pytest.raises(FileExistsError):
        c04.create_trace_arm_file(arm)
