import tempfile
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conference.journal_extension.amd_oneshot import preflight as p


def records():
    return [dict(cpu=i, online=True, allowed=True, package_id=0, core_id=i % 12,
                 thread_siblings=[i % 12, i % 12 + 12],
                 highest_perf=200 if i % 12 < 4 else 100,
                 nominal_perf=100, max_freq_khz=5000000 if i % 12 < 4 else 3500000,
                 governor='powersave', epp='balance_performance', scaling_driver='amd-pstate-epp')
            for i in range(24)]


class PreflightTests(unittest.TestCase):
    def test_missing_original_binary_precedes_source_and_cache_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(p.c03, 'git_output', side_effect=AssertionError('source must not be read')):
                with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_MISSING'):
                    p.verify_build({'repo_root': directory, 'server_bin': str(Path(directory) / 'absent')})

    def prospective_fixture(self, root, stack):
        source = root / 'llama.cpp'; build = source / 'build-diag'
        binary = build / 'bin/llama-server'; binary.parent.mkdir(parents=True)
        binary.write_bytes(b'original server'); binary.chmod(0o755)
        library = root / 'outside-bin/libllama.so'; library.parent.mkdir()
        library.write_bytes(p.c03.MARKER)
        context = source / 'src/llama-context.cpp'; context.parent.mkdir()
        context.write_bytes(p.c03.MARKER + b'\n')
        phase = root / 'conference/diagnostic/llama_cpp_phase_mark.patch'
        phase.parent.mkdir(parents=True); phase.write_text('fixture')
        compiler = root / 'compiler'; compiler.write_bytes(b'compiler')
        cache = dict(CMAKE_BUILD_TYPE='Release', GGML_NATIVE='OFF', GGML_AVX='ON',
                     GGML_BACKEND_DL='OFF', GGML_CPU_ALL_VARIANTS='OFF', GGML_BACKEND_DIR='',
                     GGML_AVX2='ON', GGML_FMA='ON', GGML_F16C='ON', GGML_AVX512='OFF',
                     GGML_OPENMP='ON', GGML_OPENMP_ENABLED='ON', BUILD_SHARED_LIBS='ON',
                     CMAKE_HOME_DIRECTORY=str(source), CMAKE_CXX_COMPILER=str(compiler))
        (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
        (build / 'compile_commands.json').write_text(json.dumps([{'command': 'c++ -mavx2 -c a.cpp'}]))
        stack.enter_context(patch.object(p, 'ORIGINAL_SERVER_SHA256', p.c03.sha256_file(binary)))
        stack.enter_context(patch.object(p, 'PHASE_PATCH_SHA256', p.c03.sha256_file(phase)))
        stack.enter_context(patch.object(p.c03, 'git_output', side_effect=lambda args, **kw: {
            'rev-parse': p.c03.PINNED_LLAMA_COMMIT, 'show': 'original',
            'diff': 'src/llama-context.cpp', 'ls-files': '' if '--others' in args else 'src/llama-context.cpp'}[args[0]]))
        stack.enter_context(patch.object(p, '_apply_frozen_patch', return_value=context.read_text()))
        closure = stack.enter_context(patch.object(p, '_runtime_closure',
            side_effect=lambda _: ([p.identity(library)], {'method': 'fixture loader'})))
        config = dict(repo_root=str(root), server_bin=str(binary), historical_identity={
            'llama_cpp_commit': p.c03.PINNED_LLAMA_COMMIT,
            'patch_sha256': p.c03.sha256_file(phase),
            'compiler_identity': 'historical compiler capture',
            'server_binary': {'sha256': p.c03.sha256_file(binary)},
            'phase_mark_capability': {'matched_file_identities': [{'sha256': p.c03.sha256_file(library)}]}})
        return config, binary, library, cache, closure

    def test_prospective_partial_historical_libraries_and_current_drift(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config, binary, library, cache, closure = self.prospective_fixture(Path(directory), stack)
            first = p.verify_build(config)
            self.assertEqual(first['provenance_mode'], 'prospective_current_identity_with_historical_anchor')
            self.assertEqual(first['shared_libraries'][0]['sha256'], p.c03.sha256_file(library))
            library.write_bytes(p.c03.MARKER + b' current drift')
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_KNOWN_LIBRARY_MISMATCH'):
                p.verify_build(config)
            binary.write_bytes(b'wrong server')
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_MISMATCH'):
                p.verify_build(config)
            binary.unlink()
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_MISSING'):
                p.verify_build(config)

    def test_prospective_resolution_failure_and_native_mismatch(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config, binary, library, cache, closure = self.prospective_fixture(Path(directory), stack)
            closure.side_effect = ValueError('ORIGINAL_BINARY_INCOMPATIBLE: resolution failed')
            with self.assertRaisesRegex(ValueError, 'INCOMPATIBLE'):
                p.verify_build(config)
            cache['GGML_NATIVE'] = 'ON'
            (binary.parent.parent / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
            with self.assertRaisesRegex(ValueError, 'GGML_NATIVE'):
                p.verify_build(config)

    def test_known_marker_anchor_allows_new_runtime_library_identities(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config, binary, library, cache, closure = self.prospective_fixture(Path(directory), stack)
            extra = Path(directory) / 'system-runtime.so'; extra.write_bytes(b'new runtime')
            closure.side_effect = lambda _: ([p.identity(library), p.identity(extra)], {'method': 'fixture'})
            first = p.verify_build(config)
            self.assertEqual(len(first['shared_libraries']), 2)
            self.assertEqual(first['historical_compiler_identity'], 'historical compiler capture')
            self.assertIn('CMAKE_CXX_COMPILER', first['compiler_file_identity'])
            extra.write_bytes(b'updated current runtime')
            second = p.verify_build(config)
            self.assertNotEqual(first['shared_libraries'], second['shared_libraries'])

    def test_dynamic_backend_loading_requires_explicit_disabled_cache_and_no_path(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config, binary, library, cache, closure = self.prospective_fixture(Path(directory), stack)
            cache_path = binary.parent.parent / 'CMakeCache.txt'
            def write_cache():
                cache_path.write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
            for key, value in [('GGML_BACKEND_DL', 'ON'), ('GGML_CPU_ALL_VARIANTS', 'ON'),
                               ('GGML_BACKEND_DIR', '/unapproved/plugins')]:
                old = cache[key]; cache[key] = value; write_cache()
                with self.assertRaisesRegex(ValueError, 'DYNAMIC_BACKEND_PROVENANCE_GAP'):
                    p.verify_build(config)
                cache[key] = old
            del cache['GGML_BACKEND_DL']; write_cache()
            with self.assertRaisesRegex(ValueError, 'DYNAMIC_BACKEND_PROVENANCE_GAP'):
                p.verify_build(config)
            cache['GGML_BACKEND_DL'] = 'OFF'; write_cache()
            with patch.dict(p.os.environ, {'GGML_BACKEND_PATH': '/extra/plugin'}):
                with self.assertRaisesRegex(ValueError, 'DYNAMIC_BACKEND_PROVENANCE_GAP'):
                    p.verify_build(config)
            self.assertIn('shared_libraries', p.verify_build(config))

    def test_loader_resolution_is_read_only_and_transitive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); binary = root / 'server'; binary.write_bytes(b'ELF')
            loader = root / 'ld.so'; loader.write_bytes(b'loader'); loader.chmod(0o755)
            runtime = root / 'libc.so'; runtime.write_bytes(b'runtime')
            results = [{'returncode': 0, 'stdout': f'[Requesting program interpreter: {loader}]'},
                       {'returncode': 0, 'stdout': f'linux-vdso.so.1 (0x1)\nlibc.so => {runtime} (0x2)\n{loader} (0x3)'}]
            with patch.object(p.c03, 'run_readonly', side_effect=results) as run:
                libraries, evidence = p._runtime_closure(binary)
            self.assertEqual(len(libraries), 2)
            self.assertEqual(run.call_args_list[1].args[0], [str(loader), '--list', str(binary)])
            results[1]['stdout'] = results[1]['stdout'].replace('0x1', '0xff').replace('0x2', '0xee')
            with patch.object(p.c03, 'run_readonly', side_effect=results):
                _, second_evidence = p._runtime_closure(binary)
            self.assertEqual(evidence, second_evidence)
            results[1]['stdout'] = 'libmissing.so => not found'
            with patch.object(p.c03, 'run_readonly', side_effect=results):
                with self.assertRaisesRegex(ValueError, 'resolution failed'):
                    p._runtime_closure(binary)

    def test_prospective_static_marker_and_executable_required(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            config, binary, library, cache, closure = self.prospective_fixture(Path(directory), stack)
            library.write_bytes(b'no phase marker')
            config['historical_identity']['phase_mark_capability']['matched_file_identities'] = []
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_INCOMPATIBLE: static'):
                p.verify_build(config)
            binary.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_INCOMPATIBLE: not executable'):
                p.verify_build(config)

    def machine(self, **overrides):
        power = dict(governors=['powersave'], epp=['balance_performance'],
                     scaling_drivers=['amd-pstate-epp'], ac_online={'AC/online': '1'},
                     warnings=[], boost={'boost': '1'}, platform_profile='balanced')
        power.update(overrides.pop('power', {}))
        def text(path):
            if str(path) == '/proc/meminfo':
                return overrides.get('meminfo', 'MemAvailable: 20000000 kB')
            return 'enabled' if overrides.get('sched_active') else '1'
        patches = [patch.object(p.os, 'sched_getaffinity', return_value=set(range(24))),
                   patch.object(p.c03, 'collect_cpu_records', return_value=records()),
                   patch.object(p.c03, 'collect_power_state', return_value=power),
                   patch.object(p.c03, 'collect_thermal_state', return_value={'selected_c': overrides.get('temp', 45)}),
                   patch.object(p, '_text', side_effect=text),
                   patch.object(p.Path, 'exists', return_value=overrides.get('sched_active', False)),
                   patch.object(p.Path, 'glob', return_value=[]),
                   patch.object(p.shutil, 'disk_usage', return_value=SimpleNamespace(free=overrides.get('disk', 100 * 2**30))),
                   patch.object(p.os, 'getloadavg', return_value=(0, 0, 0)),
                   patch.object(p, '_processes', return_value=overrides.get('processes', []))]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
        return p.check_machine_state({'repo_root': '.'}, overrides.get('expected'))

    def test_healthy_machine(self):
        self.assertEqual(self.machine()['status'], 'PASS')

    def test_ac_unknown_fails(self):
        self.assertEqual(self.machine(power={'ac_online': {}})['status'], 'NOT_READY')

    def test_heterogeneous_epp_fails(self):
        self.assertIn('missing or heterogeneous epp', self.machine(power={'epp': ['a', 'b']})['errors'])

    def test_high_temperature_fails(self):
        self.assertEqual(self.machine(temp=70)['status'], 'NOT_READY')

    def test_unreadable_temperature_fails(self):
        self.assertEqual(self.machine(temp=None)['status'], 'NOT_READY')

    def test_low_memory_fails(self):
        self.assertIn('insufficient or unreadable available memory', self.machine(meminfo='MemAvailable: 1 kB')['errors'])

    def test_low_disk_fails(self):
        self.assertIn('insufficient free disk', self.machine(disk=1)['errors'])

    def test_active_sched_ext_fails(self):
        self.assertIn('sched_ext is active or unreadable', self.machine(sched_active=True)['errors'])

    def test_per_request_temperature_is_strictly_below_80(self):
        expected = {'allowed_cpus': list(range(24)), 'power': {
            'governors': ['powersave'], 'epp': ['balance_performance'],
            'scaling_drivers': ['amd-pstate-epp'], 'boost': {'boost': '1'}, 'platform_profile': 'balanced'}}
        self.assertTrue(any('temperature' in e for e in self.machine(temp=80, expected=expected)['errors']))

    def test_power_change_fails(self):
        self.assertTrue(any('power state changed' in e for e in self.machine(expected={})['errors']))

    def test_real_ps_workload_is_not_exempt(self):
        state = self.machine(processes=[dict(pid=999, cpu_percent=90, command='ps')])
        self.assertIn('concurrent heavy CPU process', state['errors'])

    def test_patch_reconstruction_checks_context(self):
        self.assertEqual(p._apply_frozen_patch('a\nb\n', '@@ -1,2 +1,2 @@\n a\n-b\n+c\n'), 'a\nc\n')
        with self.assertRaises(ValueError):
            p._apply_frozen_patch('x\nb\n', '@@ -1,2 +1,2 @@\n a\n-b\n+c\n')

    def test_exclusive_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                p.perform_preflight({}, Path(directory))

    def test_synthetic_topology_and_frozen_identity_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'new'
            with patch.object(p.os, 'sched_getaffinity', return_value=set(range(24))), \
                 patch.object(p.c03, 'collect_cpu_records', return_value=records()), \
                 patch.object(p.c03, 'cpu_identity', return_value={'vendor_id': 'AuthenticAMD', 'model_name': 'AMD Ryzen AI 9 HX 370'}), \
                 patch.object(p, 'identity', return_value={'sha256': 'wrong'}), \
                 patch.object(p, 'verify_build', side_effect=ValueError('missing provenance')), \
                 patch.object(p, 'check_machine_state', return_value={'status': 'PASS', 'errors': [], 'warnings': []}), \
                 patch.object(p.c03, 'git_output', return_value='C04'), \
                 patch.object(p.c03, 'run_readonly', return_value={}):
                result = p.perform_preflight({'repo_root': directory, 'model': 'm', 'prompt': 'p'}, destination)
            self.assertEqual(result['status'], 'NOT_READY')
            self.assertEqual(result['topology']['masks']['COMPACT4'], [4, 5, 6, 7])
            self.assertEqual(len(result['topology']['masks']['ALL_ALLOWED4']), 12)
            self.assertEqual(result['topology']['masks']['ALL_ALLOWED4'], result['topology']['masks']['ALL_CORES12'])
            self.assertEqual(len(result['topology']['masks']['ALL_CORES12']), 12)
            self.assertEqual(result['git']['branch'], 'C04')
            self.assertTrue((destination / 'preflight.json').is_file())
            self.assertTrue(any('provenance' in e for e in result['errors']))

    def test_missing_smt_sibling_rejected(self):
        bad = records(); bad[0]['thread_siblings'] = [0]
        with self.assertRaises(p.c03.PreflightError):
            p.c03.group_physical_cores(bad)

    def test_unexpected_tracked_source_edit_rejected_with_fake_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / 'llama.cpp/src/llama-context.cpp'
            context.parent.mkdir(parents=True)
            context.write_text(p.c03.MARKER.decode() + '\n')
            frozen_patch = root / 'conference/diagnostic/llama_cpp_phase_mark.patch'
            frozen_patch.parent.mkdir(parents=True)
            frozen_patch.write_text('synthetic patch')
            binary = root / 'build/bin/llama-server'
            binary.parent.mkdir(parents=True); binary.write_bytes(b'fixture')
            def fake_git(args, cwd=None):
                if args[0] == 'rev-parse':
                    return p.c03.PINNED_LLAMA_COMMIT
                if args[0] == 'show':
                    return 'original'
                if args[0] == 'diff':
                    return 'src/llama-context.cpp\nggml/src/ggml.c'
                self.fail(f'unexpected git call: {args}')
            with patch.object(p.c03, 'git_output', side_effect=fake_git), \
                 patch.object(p, 'PHASE_PATCH_SHA256', p.c03.sha256_file(frozen_patch)), \
                 patch.object(p, '_apply_frozen_patch', return_value=context.read_text()):
                with self.assertRaisesRegex(ValueError, 'source changes exceed'):
                    p.verify_build({'repo_root': directory, 'server_bin': root / 'build/bin/llama-server'})

    def test_hidden_isa_flags_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / 'compile_commands.json').write_text(json.dumps([{'command': 'c++ -mavx2 -c a.cpp'}]))
            for flag in ('-march=native', '-mavx512f', '-march=x86-64-v4'):
                with self.assertRaisesRegex(ValueError, 'ISA'):
                    p._validate_emitted_flags({'CMAKE_CXX_FLAGS': flag}, build)
            (build / 'compile_commands.json').write_text(json.dumps([{'arguments': ['c++', '-mavx512vl', '-c', 'a.cpp']}]))
            with self.assertRaisesRegex(ValueError, 'ISA'):
                p._validate_emitted_flags({}, build)

    def test_emitted_flags_missing_is_explicit_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'provenance gap'):
                p._validate_emitted_flags({}, Path(directory))

    def test_generated_make_flags_are_supported_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            flags = build / 'ggml/CMakeFiles/ggml-cpu.dir/flags.make'
            flags.parent.mkdir(parents=True)
            flags.write_text('CXX_FLAGS = -O3 -mavx2 -mfma -mf16c\n')
            result = p._validate_emitted_flags({}, build)
            self.assertEqual(result['evidence'][0]['kind'], 'cmake_make_flags')
            self.assertEqual(result['evidence'][0]['files'][0]['sha256'], p.c03.sha256_file(flags))
            flags.write_text('CXX_FLAGS = -O3 -march=native\n')
            with self.assertRaisesRegex(ValueError, 'ISA policy mismatch'):
                p._validate_emitted_flags({}, build)

    def test_ninja_commands_are_read_only_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / 'build.ninja').write_text('# generated fixture\n')
            command = ['ninja', '-C', str(build), '-t', 'commands']
            result = {'returncode': 0, 'stdout': 'c++ -mavx2 -c source.cpp', 'command': command}
            with patch.object(p.c03, 'run_readonly', return_value=result) as run:
                verified = p._validate_emitted_flags({}, build)
                run.assert_called_once_with(command)
            self.assertEqual(verified['evidence'][0]['kind'], 'ninja_tool_commands')
            self.assertIn('commands_sha256', verified['evidence'][0])
            result['stdout'] = 'c++ -mavx512f -c source.cpp'
            with patch.object(p.c03, 'run_readonly', return_value=result):
                with self.assertRaisesRegex(ValueError, 'ISA policy mismatch'):
                    p._validate_emitted_flags({}, build)

    def test_ninja_failure_is_explicit_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / 'build.ninja').write_text('# fixture\n')
            with patch.object(p.c03, 'run_readonly', return_value={'returncode': 1, 'stdout': ''}):
                with self.assertRaisesRegex(ValueError, 'ninja commands unreadable'):
                    p._validate_emitted_flags({}, build)

    def test_complete_approved_build_and_substitutions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'llama.cpp'
            build = source / 'build-diag'; binary = build / 'bin/llama-server'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'fixture executable'); binary.chmod(0o755)
            library = binary.parent / 'libllama.so'; library.write_bytes(p.c03.MARKER)
            context = source / 'src/llama-context.cpp'; context.parent.mkdir()
            context.write_text(p.c03.MARKER.decode() + '\n')
            other = source / 'CMakeLists.txt'; other.write_text('fixture cmake\n')
            patch_path = root / 'conference/diagnostic/llama_cpp_phase_mark.patch'
            patch_path.parent.mkdir(parents=True); patch_path.write_text('fixture frozen patch')
            compiler = root / 'fixture-c++'; compiler.write_bytes(b'compiler bytes')
            cache = dict(CMAKE_BUILD_TYPE='Release', GGML_NATIVE='OFF', GGML_AVX='ON',
                         GGML_BACKEND_DL='OFF', GGML_CPU_ALL_VARIANTS='OFF', GGML_BACKEND_DIR='',
                         GGML_AVX2='ON', GGML_FMA='ON', GGML_F16C='ON', GGML_AVX512='OFF',
                         GGML_OPENMP='ON', GGML_OPENMP_ENABLED='ON', BUILD_SHARED_LIBS='ON',
                         CMAKE_HOME_DIRECTORY=str(source), CMAKE_CXX_COMPILER=str(compiler))
            (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
            (build / 'compile_commands.json').write_text(json.dumps([{'command': 'c++ -O3 -mavx2 -c context.cpp'}]))
            manifest = dict(source_commit=p.c03.PINNED_LLAMA_COMMIT, compiler_identity='fixture GNU 16.1.1',
                            source_file_sha256={n: p.c03.sha256_file(source / n) for n in ('CMakeLists.txt', 'src/llama-context.cpp')},
                            binary={'sha256': p.c03.sha256_file(binary)},
                            shared_libraries=[{'path': str(library), 'sha256': p.c03.sha256_file(library)}])
            manifest_path = root / 'approved-manifest.json'; manifest_path.write_text(json.dumps(manifest))
            config = dict(repo_root=str(root), server_bin=str(binary), build_manifest=str(manifest_path),
                          expected_build_manifest_sha256=p.c03.sha256_file(manifest_path))
            def fake_git(args, cwd=None):
                return {'rev-parse': p.c03.PINNED_LLAMA_COMMIT, 'show': 'original',
                        'diff': 'src/llama-context.cpp',
                        'ls-files': '' if '--others' in args else 'CMakeLists.txt\nsrc/llama-context.cpp'}[args[0]]
            with patch.object(p.c03, 'git_output', side_effect=fake_git), \
                 patch.object(p, 'PHASE_PATCH_SHA256', p.c03.sha256_file(patch_path)), \
                 patch.object(p, '_apply_frozen_patch', return_value=context.read_text()), \
                 patch.object(p, 'ORIGINAL_SERVER_SHA256', p.c03.sha256_file(binary)), \
                 patch.object(p, '_runtime_closure', side_effect=lambda _: ([p.identity(library)], {'method': 'fixture'})), \
                 patch.object(p.c03, 'scan_phase_mark_capability', return_value={'supported': True}):
                result = p.verify_build(config)
                self.assertEqual(result['compiler_identity'], 'fixture GNU 16.1.1')
                self.assertEqual(len(result['shared_libraries']), 1)
                for key in ('GGML_BACKEND_DL', 'GGML_CPU_ALL_VARIANTS'):
                    cache[key] = 'ON'
                    (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
                    with self.assertRaisesRegex(ValueError, 'DYNAMIC_BACKEND_PROVENANCE_GAP'):
                        p.verify_build(config)
                    cache[key] = 'OFF'
                (build / 'CMakeCache.txt').write_text(''.join(f'{k}:STRING={v}\n' for k, v in cache.items()))
                with patch.dict(p.os.environ, {'GGML_BACKEND_PATH': '/unapproved'}):
                    with self.assertRaisesRegex(ValueError, 'DYNAMIC_BACKEND_PROVENANCE_GAP'):
                        p.verify_build(config)
                for target, message in [(binary, 'ORIGINAL_BINARY_MISMATCH'), (library, 'library hash mismatch'),
                                        (other, 'source hash mismatch')]:
                    original = target.read_bytes(); target.write_bytes(b'substitution')
                    with self.assertRaisesRegex(ValueError, message):
                        p.verify_build(config)
                    target.write_bytes(original)
                original_binary = binary.read_bytes()
                binary.write_bytes(b'author-approved-manifest replacement')
                manifest['binary']['sha256'] = p.c03.sha256_file(binary)
                manifest_path.write_text(json.dumps(manifest))
                config['expected_build_manifest_sha256'] = p.c03.sha256_file(manifest_path)
                with self.assertRaisesRegex(ValueError, 'ORIGINAL_BINARY_MISMATCH'):
                    p.verify_build(config)
                binary.write_bytes(original_binary)
                manifest['binary']['sha256'] = p.c03.sha256_file(binary)
                manifest_path.write_text(json.dumps(manifest))
                config['expected_build_manifest_sha256'] = p.c03.sha256_file(manifest_path)
                config['historical_identity'] = {
                    'server_binary': {'sha256': p.c03.sha256_file(binary)},
                    'phase_mark_capability': {'matched_file_identities': [{'sha256': 'wrong-known-marker'}]}}
                with self.assertRaisesRegex(ValueError, 'ORIGINAL_KNOWN_LIBRARY_MISMATCH'):
                    p.verify_build(config)
                config['historical_identity']['phase_mark_capability']['matched_file_identities'][0]['sha256'] = p.c03.sha256_file(library)
                self.assertIn('build_manifest', p.verify_build(config))
                config['expected_build_manifest_sha256'] = 'wrong'
                with self.assertRaisesRegex(ValueError, 'manifest SHA256 mismatch'):
                    p.verify_build(config)

    def test_missing_required_tool_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(p.shutil, 'which', return_value=None), \
                 patch.object(p.c03, 'collect_cpu_records', side_effect=ValueError('synthetic unavailable')), \
                 patch.object(p, 'identity', side_effect=ValueError('synthetic missing')), \
                 patch.object(p, 'verify_build', side_effect=ValueError('synthetic missing')), \
                 patch.object(p, 'check_machine_state', return_value={'errors': [], 'warnings': []}), \
                 patch.object(p.c03, 'git_output', return_value='C04'), \
                 patch.object(p.c03, 'run_readonly', return_value={}):
                result = p.perform_preflight({'repo_root': directory, 'model': 'm', 'prompt': 'p'}, Path(directory) / 'new')
            self.assertTrue(any('required read-only tool unavailable: taskset' in e for e in result['errors']))

    def test_mask_allowed_check_independent_of_legacy_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Mock legacy validators to demonstrate the new explicit check stands alone.
            selection = {'big_cores': [{'representative_cpu': i} for i in range(4)],
                         'compact_cores': [{'representative_cpu': i, 'package_id': 0, 'core_id': i} for i in range(4, 12)]}
            with patch.object(p.os, 'sched_getaffinity', return_value={0, 1}), \
                 patch.object(p.c03, 'collect_cpu_records', return_value=records()), \
                 patch.object(p.c03, 'group_physical_cores', return_value=[]), \
                 patch.object(p.c03, 'classify_physical_cores', return_value=selection), \
                 patch.object(p.c03, 'validate_target'), patch.object(p.c03, 'validate_masks'), \
                 patch.object(p.c03, 'cpu_identity', return_value={}), \
                 patch.object(p, 'identity', return_value={'sha256': 'wrong'}), \
                 patch.object(p, 'verify_build', side_effect=ValueError('missing provenance')), \
                 patch.object(p, 'check_machine_state', return_value={'errors': [], 'warnings': []}), \
                 patch.object(p.c03, 'git_output', return_value='C04'), \
                 patch.object(p.c03, 'run_readonly', return_value={}):
                result = p.perform_preflight({'repo_root': directory, 'model': 'm', 'prompt': 'p'}, Path(directory) / 'new')
            self.assertTrue(any('outside invoking allowed affinity' in error for error in result['errors']))


if __name__ == '__main__':
    unittest.main()
