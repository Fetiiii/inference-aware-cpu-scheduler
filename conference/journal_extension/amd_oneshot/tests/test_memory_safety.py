"""Synthetic memory gates; never compile or start inference."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from conference.journal_extension.amd_oneshot import preflight as p, runtime as r, runtime_fallback as f
from conference.journal_extension.amd_oneshot.calibration import zero_shot_model


def meminfo(available):
    return ('MemTotal: 14921236 kB\n' +
            (f'MemAvailable: {int(available * 2**20)} kB\n' if available is not None else '') +
            'SwapTotal: 33554432 kB\nSwapFree: 33554432 kB\n')


class MemorySafetyTests(unittest.TestCase):
    def snapshot(self, available, floor=8, phase='pre_load'):
        with patch.object(p, '_text', return_value=meminfo(available)):
            return p.memory_snapshot(phase, floor)

    def test_actual_available_not_total_or_swap(self):
        result = self.snapshot(6.6)
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertGreater(result['total_memory_bytes'], 14 * 2**30)
        self.assertEqual(result['swap_free_bytes'], 32 * 2**30)
        self.assertTrue(result['errors'])

    def test_preload_floor_boundary_and_missing_available(self):
        for available, status in ((8, 'PASS'), (8.01, 'PASS'), (7.99, 'NOT_READY'), (None, 'NOT_READY')):
            with self.subTest(available=available):
                self.assertEqual(self.snapshot(available)['status'], status)

    def test_build_and_runtime_floors_are_separate(self):
        self.assertEqual(self.snapshot(9, 12, 'pre_build')['status'], 'NOT_READY')
        self.assertEqual(self.snapshot(2, 1, 'runtime')['status'], 'PASS')
        self.assertEqual(self.snapshot(.99, 1, 'runtime')['status'], 'NOT_READY')

    def test_default_live_snapshot_reports_memory_emergency(self):
        with patch.object(r, 'memory_snapshot', return_value=self.snapshot(.5, 1)), patch.object(r, 'package_temperature', return_value=50):
            result = r.live_safety_snapshot()
        self.assertIn('memory_emergency', result['reasons'])
        self.assertEqual(result['memory']['floor_gib'], 1)

    def test_monitor_stops_on_drop_and_preserves_sample(self):
        healthy = {'temp_c': 50, 'ac_online': True, 'reasons': []}
        low = {**healthy, 'memory': self.snapshot(.5, 1), 'reasons': ['memory_emergency']}
        abort = Mock()
        monitor = r.RequestMonitor(123, .02, Mock(), zero_shot_model(), safety_check=Mock(side_effect=[healthy, low]), abort=abort)
        with patch.object(r.time, 'monotonic', side_effect=[0, .5, 1.01]):
            monitor.check_safety(); monitor.check_safety(); monitor.check_safety()
        self.assertEqual(len(monitor.safety_samples), 2)
        self.assertTrue(monitor.abort_runtime)
        self.assertIn('memory_emergency', monitor.abort_reasons)
        abort.assert_called_once()

    def test_compilation_drop_kills_owned_group_and_saves_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'command-00.log'
            process = Mock(pid=123, wait=Mock(side_effect=[subprocess.TimeoutExpired('synthetic', 1), 0]))
            with patch.object(f.subprocess, 'Popen', return_value=process), patch.object(f.os, 'killpg') as kill, patch.object(p, 'memory_snapshot', side_effect=[self.snapshot(2, 1), self.snapshot(.5, 1)]):
                with self.assertRaisesRegex(ValueError, 'MEMORY_EMERGENCY'):
                    f._run(['synthetic'], cwd=directory, timeout=10, log_path=log, env={})
            self.assertEqual(kill.call_args_list[0].args[0], 123)
            samples = json.loads(Path(str(log) + '.memory.json').read_text())['samples']
            self.assertEqual(len(samples), 2)
            self.assertTrue(samples[-1]['errors'])


if __name__ == '__main__':
    unittest.main()
