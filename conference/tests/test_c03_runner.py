import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "conference" / "experiments" / "c03_generality.py"
SPEC = importlib.util.spec_from_file_location("c03_generality", MODULE_PATH)
C03 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = C03
SPEC.loader.exec_module(C03)


class C03RunnerTests(unittest.TestCase):
    def setUp(self):
        self.topology = C03.validate_topology([1, 3], [4, 5])

    def test_explicit_portable_topology_parsing(self):
        self.assertEqual(C03.parse_cpu_list("0-2,5,7-8"), [0, 1, 2, 5, 7, 8])
        self.assertEqual(
            C03.validate_topology([0, 2], [5, 7], online=range(8), allowed=range(8)),
            {"big": [0, 2], "compact": [5, 7], "all": [0, 2, 5, 7]},
        )
        with self.assertRaises(ValueError):
            C03.validate_topology([0, 2], [2, 4])

    def test_c03_logic_contains_no_intel_magic_cpu_ranges(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = (
            "range(0, 16)", "range(16, 24)", "P_CORES_NOSMT",
            "P_CORES_ALL", "E_CORES",
        )
        self.assertTrue(all(token not in source for token in forbidden))

    def test_randomized_round_contains_both_arms_exactly_once(self):
        schedule = C03.build_schedule(2, C03.DEFAULT_ORDER_SEED)
        for round_number in (1, 2):
            members = [item for item in schedule if item["round"] == round_number]
            self.assertEqual(len(members), 2)
            self.assertEqual({item["arm"] for item in members}, set(C03.ARMS))
            self.assertEqual(
                {item["randomized_order_seed"] for item in members},
                {C03.DEFAULT_ORDER_SEED + round_number},
            )
        self.assertEqual(
            [item["arm"] for item in schedule],
            ["ALL_CORES", "BIG_ONLY", "BIG_ONLY", "ALL_CORES"],
        )

    def test_zero_shot_preserves_frozen_detector_parameters(self):
        config = C03.validate_detector_config(
            "zero_shot", 20.0, 3000.0, 2100.0, 2
        )
        self.assertTrue(config["frozen_intel_parameters_unchanged"])
        self.assertEqual(config["mode"], "zero_shot")
        with self.assertRaises(ValueError):
            C03.validate_detector_config("zero_shot", 20, 2900, 2100, 2)

    def test_recalibration_cannot_be_mislabeled_zero_shot(self):
        with self.assertRaises(ValueError):
            C03.validate_detector_config(
                "recalibrated", 20, 2800, 1900, 2, recalibration_label=""
            )
        config = C03.validate_detector_config(
            "recalibrated", 20, 2800, 1900, 2,
            recalibration_label="platform_calibration_v1",
        )
        self.assertEqual(config["mode"], "recalibrated")
        self.assertFalse(config["frozen_intel_parameters_unchanged"])

    def test_internal_marker_cannot_influence_external_detector(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "server.log"
            log_path.write_text("", encoding="utf-8")
            monitor, watcher = C03.build_observers(
                123, log_path, 0,
                C03.validate_detector_config("zero_shot", 20, 3000, 2100, 2),
            )
        self.assertIsNone(monitor.external_callback)
        self.assertIsNone(watcher.decision_callback)
        self.assertFalse(hasattr(monitor.actuator, "apply"))

    def test_arm_metadata_records_exact_masks(self):
        specs = C03.arm_specs(self.topology, 2, 4)
        self.assertEqual(specs["BIG_ONLY"]["cpu_mask"], [1, 3])
        self.assertEqual(specs["ALL_CORES"]["cpu_mask"], [1, 3, 4, 5])
        self.assertEqual(specs["BIG_ONLY"]["threads"], 2)
        self.assertEqual(specs["ALL_CORES"]["threads"], 4)

    def test_selected_generality_path_is_explicit(self):
        self.assertEqual(C03.validate_selected_path("CROSS_VENDOR", ""), "CROSS_VENDOR")
        self.assertEqual(
            C03.validate_selected_path("FALLBACK_MODEL", "different-family"),
            "FALLBACK_MODEL",
        )

    def test_cross_vendor_and_fallback_paths_cannot_both_execute(self):
        with self.assertRaises(ValueError):
            C03.validate_selected_path("CROSS_VENDOR", "fallback-family")
        with self.assertRaises(ValueError):
            C03.validate_selected_path("FALLBACK_MODEL", "")

    def test_selected_path_rejects_wrong_known_vendor(self):
        with self.assertRaises(RuntimeError):
            C03.validate_hardware_for_path(
                "CROSS_VENDOR", {"vendor_id": "GenuineIntel"}
            )
        C03.validate_hardware_for_path(
            "CROSS_VENDOR", {"vendor_id": "AuthenticAMD"}
        )

    def test_missing_phase_marker_is_rejected_before_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain-server"
            plain.write_bytes(b"no diagnostic marker")
            with self.assertRaises(RuntimeError):
                C03.verify_phase_mark_binary(plain)
            diagnostic = Path(tmp) / "diag-server"
            diagnostic.write_bytes(b"prefix PHASE_MARK suffix")
            self.assertTrue(C03.verify_phase_mark_binary(diagnostic))

    def test_phase_marker_may_reside_in_sibling_shared_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "llama-server"
            binary.write_bytes(b"launcher")
            library = Path(tmp) / "libllama.so.0"
            library.write_bytes(b"PHASE_MARK")
            self.assertTrue(C03.verify_phase_mark_binary(binary))

    def test_plan_preserves_selected_path_topology_and_schedule(self):
        args = SimpleNamespace(
            path="CROSS_VENDOR", rounds=2, order_seed=3304,
            detector_config=C03.validate_detector_config(
                "zero_shot", 20, 3000, 2100, 2
            ),
        )
        specs = C03.arm_specs(self.topology, 2, 4)
        with tempfile.TemporaryDirectory() as tmp:
            plan = C03.ensure_plan(tmp, args, self.topology, specs)
            self.assertEqual(plan["selected_c03_path"], "CROSS_VENDOR")
            self.assertEqual(plan["topology"], self.topology)
            self.assertEqual(len(plan["schedule"]), 4)

    def test_argument_validation_refuses_non_smoke_round_count(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                C03.parse_args([
                    "--path", "CROSS_VENDOR", "--big-cpus", "0",
                    "--compact-cpus", "1", "--threads-big", "1",
                    "--threads-all", "2", "--server-bin", "missing",
                    "--model", "missing", "--rounds", "6",
                ])

    def test_six_rounds_accepted_with_approval_flag(self):
        detector = C03.validate_detector_config("zero_shot", 20, 3000, 2100, 2)
        args_smoke = SimpleNamespace(
            path="CROSS_VENDOR", rounds=2, order_seed=3304, detector_config=detector,
        )
        args_pilot = SimpleNamespace(
            path="CROSS_VENDOR", rounds=6, order_seed=3304, detector_config=detector,
        )
        specs = C03.arm_specs(self.topology, 2, 4)
        with tempfile.TemporaryDirectory() as tmp:
            plan_smoke = C03.ensure_plan(tmp, args_smoke, self.topology, specs)
            self.assertEqual(len(plan_smoke["schedule"]), 4)
            plan_pilot = C03.ensure_plan(tmp, args_pilot, self.topology, specs)
            self.assertEqual(len(plan_pilot["schedule"]), 12)
            self.assertEqual(plan_pilot["schedule"][:4], plan_smoke["schedule"])
            self.assertEqual(len(C03.schedule_from_round(plan_pilot, 3)), 8)


if __name__ == "__main__":
    unittest.main()
