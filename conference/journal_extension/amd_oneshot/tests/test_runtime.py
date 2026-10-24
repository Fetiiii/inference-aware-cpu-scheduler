"""Study backend tests: no real processes, sockets, or inference/network I/O."""
import tempfile
import importlib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from conference.journal_extension.amd_oneshot import runtime as rt
from conference.journal_extension.amd_oneshot.calibration import zero_shot_model


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        memory = patch.object(rt, 'memory_snapshot', return_value={
            'status': 'PASS', 'errors': [], 'available_memory_bytes': 16 * 2**30})
        memory.start()
        self.addCleanup(memory.stop)

    def spec(self, source="none"):
        return dict(cpus=None if source == "none" else [2, 9, 21, 44],
                    target_cpus=[9, 44], threads=4, threads_batch=4, phase_source=source)

    def test_stock_command_has_no_affinity_and_cpu_only(self):
        command = rt.server_command(dict(server_bin="server", model="model"), self.spec(), 8130)
        self.assertNotIn("taskset", command)
        self.assertEqual(command[command.index("-ngl") + 1], "0")

    def test_import_never_starts_inference_process_or_network(self):
        with patch.object(rt.subprocess, "Popen") as process, patch.object(rt.ro, "post_json") as network, patch.object(rt.socket, "socket") as sock:
            importlib.reload(rt)
            process.assert_not_called()
            network.assert_not_called()
            sock.assert_not_called()

    def test_stream_timeout_preserves_partial_tokens_without_retry(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read1.side_effect = [b'data: {"content": "a"}\ndata: {"content": "b"}\n', TimeoutError("deadline")]
        tokens = []
        with patch.object(rt.ro, "post_json", return_value=connection):
            with self.assertRaises(rt.RuntimeFailure) as failure:
                rt._stream(1234, {"stream": True}, .5, tokens)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(failure.exception.tokens_seen, 2)
        self.assertFalse(failure.exception.retryable)
        connection.close.assert_called_once_with()
        connection.sock.settimeout.assert_called()
        self.assertLessEqual(connection.sock.settimeout.call_args.args[0], .5)

    def test_stream_retains_final_frame_and_closes_owned_connection(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read1.side_effect = [b'data: {"content": "a"}\ndata: {"content": "b"}\ndata: {"stop": true}\n', b'']
        tokens = []
        with patch.object(rt.ro, "post_json", return_value=connection):
            _, text, final = rt._stream(1234, {}, .5, tokens)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(text, "ab")
        self.assertTrue(final["stop"])
        connection.close.assert_called_once_with()

    def test_cpu_class_uses_inventory_not_number(self):
        groups = {"big": [44], "compact": [2]}
        self.assertEqual(rt.cpu_class(44, groups), "big")
        self.assertEqual(rt.cpu_class(2, groups), "compact")
        self.assertEqual(rt.cpu_class(9, groups), "unclassified")

    def test_marker_only_routes_when_given_oracle_callback(self):
        for source in ("none", "external_proc", "internal_oracle"):
            action = Mock()
            watcher = rt.c02.PhaseMarkWatcher("unused", 0,
                action if source == "internal_oracle" else None)
            watcher.observe_line("PHASE_MARK batched=0 t_mono_ns=123")
            self.assertEqual(action.call_count, int(source == "internal_oracle"))

    def test_marker_blind_detector_and_actual_cpu_samples(self):
        actuator = Mock(record=None)
        route = Mock()
        monitor = rt.RequestMonitor(123, .02, actuator, zero_shot_model(), route)
        monitor.decision_start_ns = 1
        detector = Mock(state="decode")
        detector.feed.return_value = dict(state="decode", t_ns=20)
        monitor.detector = detector
        with patch.object(rt, "live_safety_snapshot", return_value=dict(ac_online=True, temp_c=50)), patch.object(rt.tr, "sample_tasks", return_value={7: dict(cpu=44, state="R", cpu_ticks=2)}), patch.object(rt, "frequency_snapshot", return_value={}):
            monitor.samples.append(dict(t_ns=20, norm_ctx_per_cpu_s=5000, cputime_jiffies=1, ctx_switches=1))
        detector.feed.assert_called_once_with(dict(t_ns=20, norm_ctx_per_cpu_s=5000))
        route.assert_called_once_with(20, "external_proc")
        self.assertEqual(monitor.samples[0]["tasks"][7]["cpu"], 44)
        self.assertGreaterEqual(monitor.samples[0]["total_observation_us"], monitor.samples[0]["extra_observation_us"])

    def test_safety_stops_only_owned_server_immediately(self):
        for snapshot, reason in ((dict(ac_online=True, temp_c=95), "critical_temperature"),
                                 (dict(ac_online=True, temp_c=None), "temperature_unreadable"),
                                 (dict(ac_online=None, temp_c=50), "ac_disconnected_or_unknown"),
                                 (dict(ac_online=False, temp_c=50), "ac_disconnected_or_unknown")):
            with self.subTest(snapshot=snapshot):
                own_stop = Mock()
                monitor = rt.RequestMonitor(999, .02, Mock(), zero_shot_model(),
                    safety_check=Mock(return_value=snapshot), abort=own_stop)
                monitor.check_safety()
                self.assertTrue(monitor.abort_runtime)
                self.assertIn(reason, monitor.abort_reasons)
                own_stop.assert_called_once_with()

    def test_free_port_check_refuses_existing_service(self):
        reservation = Mock()
        reservation.bind.side_effect = OSError("address in use")
        with patch.object(rt.socket, "socket", return_value=reservation):
            with self.assertRaises(OSError):
                rt.reserve_port(8130)
        reservation.close.assert_called_once_with()

    def test_trace_requires_samples_on_both_sides_and_monotonic_time(self):
        tasks = {7: dict(cpu=44, state="R", cpu_ticks=2)}
        samples = [dict(t_ns=t*1_000_000, tasks=tasks, norm_ctx_per_cpu_s=2)
                   for t in (110, 130, 150, 850, 870, 890)]
        result = rt.validate_trace(samples, 100_000_000, 900_000_000, 500_000_000)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["gaps_over_50ms"]), 1)
        self.assertFalse(rt.validate_trace(samples[:3], 100_000_000, 900_000_000, 500_000_000)["valid"])
        self.assertFalse(rt.validate_trace(samples[::-1], 100_000_000, 900_000_000, 500_000_000)["valid"])

    def test_owned_server_cleaned_and_partial_artifacts_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            proc = Mock()
            proc.poll.return_value = None
            with patch.object(rt, "reserve_port", return_value=(Mock(), 1234)), patch.object(rt.subprocess, "Popen", return_value=proc), patch.object(rt.ro, "wait_for_health", side_effect=RuntimeError("timeout")), patch.object(rt.c02.c01, "terminate_process_group") as cleanup, patch.object(rt.os, "sched_setaffinity") as affinity:
                with self.assertRaises(rt.RuntimeFailure) as failure:
                    rt.run_request(dict(server_bin="server", model="model", prompt="unused"), dict(arm="STOCK"), self.spec(), out, zero_shot_model())
                self.assertTrue(failure.exception.retryable)
                cleanup.assert_called_once_with(proc)
                affinity.assert_not_called()
                self.assertTrue((out / "record.json").is_file())
                self.assertTrue((out / "server.log").is_file())

    def test_partial_connection_failure_not_retryable(self):
        with patch.object(rt.ro, "post_json", side_effect=ConnectionResetError):
            with self.assertRaises(rt.RuntimeFailure) as failure:
                rt._stream(1, {}, 1, [123])
            self.assertFalse(failure.exception.retryable)
            self.assertEqual(failure.exception.tokens_seen, 1)

    def test_success_all_sources_common_actuator_and_strict_routes(self):
        for source, arm, missed in (("none", "STOCK", False), ("external_proc", "EXTERNAL", False),
                            ("internal_oracle", "ORACLE", False), ('external_proc', 'EXTERNAL', True)):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                prompt = Path(directory) / "prompt"
                prompt.write_text("test prompt")
                config = dict(server_bin="server", model="model", prompt=prompt, n_predict=2)
                proc = Mock(pid=123)
                proc.poll.return_value = 0
                actuator = Mock(record=None)
                if source != "none" and not missed:
                    actuator.record = dict(n_tids_succeeded=4, n_tids_failed=0, t_trigger_ns=110,
                                           t_affinity_done_ns=530_000_000,
                                           t_affinity_start_ns=529_900_000, affinity_cost_us=100.)
                tasks = {7: dict(cpu=44, state="R", cpu_ticks=2)}
                monitor = Mock(samples=[dict(t_ns=t*1_000_000, tasks=tasks, norm_ctx_per_cpu_s=2)
                                        for t in (110, 130, 150, 850, 870, 890)],
                               decisions=[], error=None, actuator=actuator, abort_runtime=False,
                               abort_reasons=[], safety_samples=[], thread_cpu_ns=123,
                               external_detect_ns=None if missed else 520_000_000)
                monitor.arm_for_request.return_value = 100_000_000
                monitor.is_alive.return_value = False
                watcher = Mock(t_internal_phase_ns=500_000_000, t_marker_seen_ns=510_000_000, markers=[])
                watcher.is_alive.return_value = False
                constructor = stack.enter_context(patch.object(rt, "RequestMonitor", return_value=monitor))
                marker_constructor = stack.enter_context(patch.object(rt.c02, "PhaseMarkWatcher", return_value=watcher))
                action_constructor = stack.enter_context(patch.object(rt.c02, "AffinityActuator", return_value=actuator))
                stack.enter_context(patch.object(rt, "reserve_port", return_value=(Mock(), 1234)))
                stack.enter_context(patch.object(rt.time, "sleep"))
                stack.enter_context(patch.object(rt, "affinity_snapshot", return_value={
                    "main_thread": self.spec(source)["cpus"] or [2, 9, 21, 44], "per_thread": {}}))
                stack.enter_context(patch.object(rt.subprocess, "Popen", return_value=proc))
                stack.enter_context(patch.object(rt.ro, "wait_for_health"))
                cleanup = stack.enter_context(patch.object(rt.c02.c01, "terminate_process_group"))
                affinity = stack.enter_context(patch.object(rt.os, "sched_setaffinity"))
                stack.enter_context(patch.object(rt.bl, "sched_snapshot", return_value={}))
                stack.enter_context(patch.object(rt, "package_temperature", return_value=50))
                stack.enter_context(patch.object(rt, "frequency_snapshot", return_value={44: 3300}))
                stack.enter_context(patch.object(rt.c02, "thermal_throttle_snapshot", return_value={}))
                stack.enter_context(patch.object(rt.c02, "parse_phase_markers_offline", return_value=[dict(batched=0, t_mono_ns=500_000_000)]))
                if actuator.record is not None:
                    actuator.record["t_trigger_ns"] = 500_000_000
                def stream(port, payload, timeout, tokens):
                    if payload["n_predict"] == 2:
                        tokens.extend([510_000_000, 900_000_000])
                    return 100_000_000, "ab", {"stop": True}
                streaming = stack.enter_context(patch.object(rt, "_stream", side_effect=stream))
                record = rt.run_request(config, dict(arm=arm), self.spec(source), Path(directory) / "run", zero_shot_model())
                self.assertEqual(record["status"], "ok")
                self.assertEqual(record["threads"], 4)
                self.assertEqual(record["threads_batch"], 4)
                self.assertEqual(record["t_external_detect_ns"], None if missed else 520_000_000)
                if missed:
                    self.assertEqual(record['policy_action_status'], 'NO_EXTERNAL_TRIGGER')
                self.assertEqual(record["marker_delivery_ms"], 10.)
                self.assertEqual(record["detect_offset_ms"], None if missed else 20.)
                self.assertIn("backend_total_elapsed_s", record)
                self.assertNotIn("model_load_s", record["timings"])
                action_constructor.assert_called_once_with(123, [9, 44])
                self.assertEqual(constructor.call_args.args[-1], actuator.apply if source == "external_proc" else None)
                self.assertEqual(marker_constructor.call_args.args[-1], actuator.apply if source == "internal_oracle" else None)
                affinity.assert_not_called()
                cleanup.assert_called_once_with(proc)
                self.assertFalse(streaming.call_args_list[0].args[1]["cache_prompt"])
                self.assertEqual(record["per_tid_cpu_observations"]["7"]["cpu_observations"], {"44": 6})
                self.assertEqual(len(record["trace_validation"]["gaps_over_50ms"]), 1)

    def test_keyboard_interrupt_owns_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Mock()
            proc.poll.return_value = 0
            with patch.object(rt, "reserve_port", return_value=(Mock(), 1234)), patch.object(rt.subprocess, "Popen", return_value=proc), patch.object(rt.ro, "wait_for_health", side_effect=KeyboardInterrupt), patch.object(rt.c02.c01, "terminate_process_group") as cleanup:
                with self.assertRaises(KeyboardInterrupt):
                    rt.run_request(dict(server_bin="server", model="model"), dict(arm="STOCK"), self.spec(), Path(directory) / "run", zero_shot_model())
                cleanup.assert_called_once_with(proc)


if __name__ == "__main__":
    unittest.main()
