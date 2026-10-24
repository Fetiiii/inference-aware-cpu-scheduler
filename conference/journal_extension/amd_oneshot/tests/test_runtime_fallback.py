"""Synthetic offline fixtures: no compiler, configure or server execution."""
import copy
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from conference.journal_extension.amd_oneshot import preflight, protocol, runtime_fallback as f
from conference.journal_extension.amd_oneshot.common import digest, save_json


class RuntimeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-runtime-fallback-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'; self.local = self.repo / 'llama.cpp'
        (self.local / 'src').mkdir(parents=True)
        (self.local / 'src/llama-context.cpp').write_text('original\n')
        (self.local / 'CMakeLists.txt').write_text('# synthetic local CMake\n')
        self.phase = self.repo / f.POLICY['phase_patch']
        self.phase.parent.mkdir(parents=True)
        self.phase.write_text('@@ -1 +1,2 @@\n original\n+' + preflight.c03.MARKER.decode() + '\n')
        patcher = patch.dict(f.POLICY, {'phase_patch_sha256': preflight.c03.sha256_file(self.phase)})
        patcher.start(); self.addCleanup(patcher.stop)
        policy_hash_patch = patch.object(f, 'POLICY_SHA256', digest(f.POLICY))
        policy_hash_patch.start(); self.addCleanup(policy_hash_patch.stop)
        self.compiler = self.root / 'existing-tool'; self.compiler.write_bytes(b'existing compiler')
        self.compiler.chmod(0o755)
        self.plan = json.loads(protocol.PLAN_PATH.read_text())
        self.plan['runtime_fallback_policy'] = copy.deepcopy(f.POLICY)
        self.config = {'repo_root': str(self.repo), 'plan': self.plan, 'package_sha256': 'synthetic-package',
                       'archive_reserve_s': 180, 'server_bin': str(self.repo / 'missing-original')}
        self.approval = {'authorized': True, 'runtime_fallback_authorized': True,
                         'author_review_id': 'SYNTHETIC-ONLY', 'plan_sha256': digest(self.plan),
                         'package_sha256': self.config['package_sha256'],
                         'maximum_session_s': self.plan['maximum_session_s'],
                         **protocol.approval_bindings(self.plan)}
        self.commands = []
        self.now = 1000.
        self.work = self.root / 'isolated-runtime'
        self.patchers = [patch.object(f.shutil, 'which', return_value=str(self.compiler)),
                         patch.object(preflight, 'memory_snapshot', return_value={'status': 'PASS', 'errors': [], 'available_memory_bytes': 16 * 2**30}),
                         patch.object(preflight.c03, 'git_output', return_value=f.POLICY['source_revision']),
                         patch.object(preflight, '_runtime_closure', side_effect=self.closure)]
        for item in self.patchers:
            item.start(); self.addCleanup(item.stop)

    def closure(self, binary):
        return [preflight.identity(binary.parent / 'libllama.so')], {'method': 'synthetic loader list'}

    def runner(self, command, **kwargs):
        self.commands.append((command, kwargs))
        Path(kwargs['log_path']).write_text('synthetic command output\n')
        if 'archive' in command:
            target = Path(next(value.split('=', 1)[1] for value in command if value.startswith('--output=')))
            with tarfile.open(target, 'w') as bundle:
                for file in self.local.rglob('*'):
                    if file.is_file():
                        bundle.add(file, arcname=str(file.relative_to(self.local)))
        elif '-S' in command:
            source = Path(command[command.index('-S') + 1]); build = Path(command[command.index('-B') + 1])
            build.mkdir()
            cache = {value[2:].split('=', 1)[0]: value.split('=', 1)[1]
                     for value in command if value.startswith('-D')}
            cache.update(CMAKE_HOME_DIRECTORY=str(source), GGML_OPENMP_ENABLED='ON')
            (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
            (build / 'compile_commands.json').write_text(json.dumps([{'command': 'c++ -O3 -mavx2 -c context.cpp'}]))
        elif '--build' in command:
            build = Path(command[command.index('--build') + 1])
            (build / 'bin').mkdir()
            binary = build / 'bin/llama-server'; binary.write_bytes(b'new prospective runtime'); binary.chmod(0o755)
            (binary.parent / 'libllama.so').write_bytes(preflight.c03.MARKER)
        self.now += 1

    def build(self, **kwargs):
        return f.build_fallback(self.config, self.approval, self.work, 990,
                                clock=lambda: self.now, runner=kwargs.get('runner', self.runner))

    def test_approved_new_prospective_identity_and_full_current_guard(self):
        result = self.build()
        config = {**self.config, **result['config_overrides']}
        fingerprint = preflight.verify_build(config)
        self.assertEqual(fingerprint['provenance_mode'], 'new_prospective_author_approved_runtime_fallback')
        self.assertFalse(fingerprint['historical_binary_equivalent'])
        self.assertIn('synthetic command output', result['manifest']['compiler_version_capture']['CXX'])
        self.assertNotEqual(fingerprint['binary']['sha256'], preflight.ORIGINAL_SERVER_SHA256)
        self.assertEqual((self.local / 'src/llama-context.cpp').read_text(), 'original\n')
        binary = Path(config['server_bin']); binary.write_bytes(b'manual substitution')
        with self.assertRaisesRegex(ValueError, 'CURRENT_IDENTITY_MISMATCH'):
            preflight.verify_build(config)

    def test_build_entry_rechecks_twelve_gib_before_any_command(self):
        with patch.object(preflight, 'memory_snapshot', return_value={
                'phase': 'pre_build', 'floor_gib': 12, 'available_memory_bytes': 9 * 2**30,
                'status': 'NOT_READY', 'errors': ['insufficient or unreadable available memory']}) as snapshot:
            with self.assertRaisesRegex(ValueError, 'INSUFFICIENT_BUILD_MEMORY'):
                self.build()
        snapshot.assert_called_once_with('pre_build', 12)
        self.assertFalse(self.commands)
        self.assertTrue((self.work / 'runtime-fallback-failure.json').is_file())
        self.assertTrue((self.work / 'command-pre-build.memory.json').is_file())

    def test_unapproved_policy_or_source_plan_changes_rejected_before_any_command(self):
        for key, value in [('runtime_fallback_authorized', False),
                           ('runtime_fallback_policy_sha256', 'wrong'), ('plan_sha256', 'wrong'),
                           ('package_sha256', 'wrong')]:
            approval = {**self.approval, key: value}
            with self.assertRaises(ValueError):
                f.build_fallback(self.config, approval, self.work, 990, clock=lambda: self.now, runner=self.runner)
        self.assertFalse(self.commands)
        self.assertFalse(self.work.exists())

    def test_manifest_or_authorization_tamper_and_manual_binary_path_rejected(self):
        result = self.build(); config = {**self.config, **result['config_overrides']}
        changed = {**config, 'expected_runtime_fallback_manifest_sha256': 'wrong'}
        with self.assertRaisesRegex(ValueError, 'MANIFEST_MISMATCH'):
            preflight.verify_build(changed)
        changed = {**config, 'server_bin': str(self.compiler)}
        with self.assertRaisesRegex(ValueError, 'BINARY_SUBSTITUTION'):
            preflight.verify_build(changed)
        changed = {**config, 'runtime_fallback_approval': {**self.approval, 'author_review_id': 'changed'}}
        with self.assertRaisesRegex(ValueError, 'APPROVAL_MISMATCH'):
            preflight.verify_build(changed)
        Path(config['runtime_fallback_manifest']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'MANIFEST_MISMATCH'):
            preflight.verify_build(config)

    def test_commands_are_local_offline_and_runtime_is_never_started(self):
        self.build()
        commands = [command for command, _ in self.commands]
        self.assertEqual(len(commands), 5)
        self.assertIn('archive', commands[0]); self.assertIn(f.POLICY['source_revision'], commands[0])
        configure = commands[1]
        for value in ('-DLLAMA_BUILD_UI=OFF', '-DLLAMA_USE_PREBUILT_UI=OFF',
                      '-DFETCHCONTENT_FULLY_DISCONNECTED=ON', '-DGGML_NATIVE=OFF',
                      '-DGGML_BACKEND_DL=OFF', '-DGGML_CPU_ALL_VARIANTS=OFF'):
            self.assertIn(value, configure)
        self.assertTrue(all(command[0] != str(self.work / 'build/bin/llama-server') for command in commands))
        self.assertTrue(all(0 < kwargs['timeout'] <= 900 for _, kwargs in self.commands))

    def test_budget_missing_prerequisites_and_isolation_fail_closed(self):
        self.now = 12000
        with self.assertRaisesRegex(ValueError, 'BUDGET_EXHAUSTED'):
            self.build()
        self.now = 1000
        with patch.object(f.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'PREREQUISITE_MISSING'):
                self.build()
        with self.assertRaisesRegex(ValueError, 'ISOLATION'):
            f.build_fallback(self.config, self.approval, self.local / 'bad-build', 990)
        self.assertFalse(self.commands)

    def test_timeout_or_cancellation_preserves_failure_evidence(self):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.build(runner=timeout)
        failure = json.loads((self.work / 'runtime-fallback-failure.json').read_text())
        self.assertEqual(failure['status'], 'FAILED')
        self.assertEqual(failure['type'], 'TimeoutExpired')

    def test_build_timeout_uses_remaining_global_budget_and_reserved_archive_time(self):
        self.now = 10500.
        f.build_fallback(self.config, self.approval, self.work, 0,
                         clock=lambda: self.now, runner=self.runner)
        self.assertTrue(all(kwargs['timeout'] <= 120 for _, kwargs in self.commands))

    def test_altered_phase_patch_is_rejected_before_any_command(self):
        self.phase.write_text('unreviewed instrumentation')
        with self.assertRaisesRegex(ValueError, 'phase patch hash mismatch'):
            self.build()
        self.assertFalse(self.commands)

    def test_wall_clock_rollback_stops_before_next_command(self):
        def rollback(command, **kwargs):
            self.runner(command, **kwargs)
            self.now = 999.
        with self.assertRaisesRegex(ValueError, 'CLOCK_ROLLBACK'):
            f.build_fallback(self.config, self.approval, self.work, 990,
                             clock=lambda: self.now, monotonic=lambda: 100., runner=rollback)
        self.assertEqual(len(self.commands), 1)

    def test_monotonic_clock_caps_build_despite_frozen_wall_clock(self):
        mono = [100.]
        def advance(command, **kwargs):
            self.runner(command, **kwargs)
            mono[0] += 901
        with self.assertRaisesRegex(ValueError, 'BUDGET_EXHAUSTED'):
            f.build_fallback(self.config, self.approval, self.work, 990,
                             clock=lambda: 1000., monotonic=lambda: mono[0], runner=advance)
        self.assertEqual(len(self.commands), 1)

    def test_invalid_session_start_is_rejected_without_commands(self):
        for start in (float('nan'), float('inf'), -1, 1001, True):
            with self.assertRaisesRegex(ValueError, 'INVALID_START_TIME'):
                f.build_fallback(self.config, self.approval, self.work, start,
                                 clock=lambda: 1000., runner=self.runner)
        self.assertFalse(self.commands)

    def test_runner_terminates_process_group_on_cancellation(self):
        process = Mock(pid=123, wait=Mock(side_effect=[KeyboardInterrupt(), 0]))
        with patch.object(f.subprocess, 'Popen', return_value=process) as popen, patch.object(f.os, 'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                f._run(['synthetic-cmake'], cwd=self.root, timeout=1,
                       log_path=self.root / 'cancel.log', env={})
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(kill.call_args_list[0].args, (123, f.signal.SIGTERM))
            self.assertEqual(kill.call_args_list[-1].args, (123, f.signal.SIGKILL))


if __name__ == '__main__':
    unittest.main()
