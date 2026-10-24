"""Read-only, fail-closed checks for a new AMD dataset (never runs inference)."""
import hashlib
import json
import math
import os
import platform
import re
import shutil
from pathlib import Path

from conference.tools import c03_amd_preflight as c03

MODEL_SHA256 = "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8"
PROMPT_SHA256 = "27f3f8ba7b73d3df1337549237a8e9f4ccf57e0551908cc93ce8a183480751b8"
ORIGINAL_SERVER_SHA256 = 'a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1'
PHASE_PATCH_SHA256 = '722adc5c8d8805d442753971d2eabd488ab91906aa8fa93961d6a615194833ce'
POWER_FIELDS = ("scaling_drivers", "governors", "epp", "amd_pstate_status",
                "amd_pstate_prefcore", "boost", "platform_profile", "powerprofilesctl")


def identity(path):
    result = c03.file_identity(path)
    stat = Path(path).stat()
    result.update(inode=stat.st_ino, device=stat.st_dev)
    return result


def _text(path):
    return c03.read_text(path)


def _processes():
    # Exclude only our own observer tree; a real workload named ps still counts.
    result = c03.run_readonly(["ps", "-eo", "pid=,ppid=,pcpu=,comm=", "--sort=-pcpu"])
    if result["returncode"] != 0:
        raise ValueError("cannot inspect concurrent CPU processes")
    rows = []
    for line in result["stdout"].splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            rows.append(dict(pid=int(fields[0]), ppid=int(fields[1]),
                             cpu_percent=float(fields[2]), command=fields[3]))
    return [row for row in rows if row["pid"] != os.getpid()
            and not (row["ppid"] == os.getpid() and row["command"] == "ps")][:30]


def memory_snapshot(phase='pre_load', floor_gib=8):
    """Observe physical available memory; swap never increases the headroom."""
    errors = []
    try:
        text = _text('/proc/meminfo') or ''
    except OSError as exc:
        text = ''
        errors.append('memory read failed: ' + str(exc))
    fields = {}
    for name in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
        match = re.search(r'^' + name + r':\s+(\d+)\s+kB\s*$', text, re.M)
        fields[name] = int(match.group(1)) * 1024 if match else None
    try:
        floor = float(floor_gib) * 2**30
        if not math.isfinite(floor) or floor <= 0 or isinstance(floor_gib, bool):
            raise ValueError('invalid floor')
    except (TypeError, ValueError, OverflowError):
        floor = None
        errors.append('invalid memory floor')
    if fields['MemAvailable'] is None or (floor is not None and fields['MemAvailable'] < floor):
        errors.append('insufficient or unreadable available memory')
    return {'phase': phase, 'floor_gib': floor_gib, 'floor_bytes': floor,
            'total_memory_bytes': fields['MemTotal'],
            'available_memory_bytes': fields['MemAvailable'],
            'swap_total_bytes': fields['SwapTotal'], 'swap_free_bytes': fields['SwapFree'],
            'errors': errors, 'status': 'NOT_READY' if errors else 'PASS'}


def check_machine_state(config, expected=None):
    """Recheck per-request gates; expected is a preceding machine-state report."""
    errors, warnings = [], []
    state = {"errors": errors, "warnings": warnings, "kernel": platform.release()}
    try:
        allowed = sorted(os.sched_getaffinity(0))
        records = c03.collect_cpu_records(allowed=allowed)
        power = c03.collect_power_state(records)
        thermal = c03.collect_thermal_state()
        state.update(allowed_cpus=allowed, logical_cpus=records, power=power, thermal=thermal,
                     smt_active=_text('/sys/devices/system/cpu/smt/active'))
        warnings.extend(power.get("warnings", []))
        if not power.get("ac_online") or not any(v == "1" for v in power["ac_online"].values()):
            errors.append("AC power absent, unknown, or on battery")
        for field in ("governors", "epp", "scaling_drivers"):
            if len(power.get(field, [])) != 1:
                errors.append(f"missing or heterogeneous {field}")
        if any(not r.get("governor") or not r.get("epp") or not r.get("scaling_driver") for r in records):
            errors.append("incomplete per-CPU power state")
        temperature = thermal.get("selected_c")
        limit = 80.0 if expected is not None else float(config.get("preflight_temp_max_c", 70))
        if temperature is None or temperature >= limit:
            errors.append(f"temperature unreadable or >= {limit:g} C")
        if temperature is not None and temperature >= float(config.get("critical_temp_c", 100)):
            errors.append("critical temperature reached")
        memory = memory_snapshot(config.get('memory_phase', 'pre_load'),
                                 config.get('min_available_memory_gib', 8))
        state['memory'] = memory
        errors.extend(memory['errors'])
        disk_path = Path(config.get("disk_path", config.get("repo_root", '.')))
        free = shutil.disk_usage(disk_path).free
        state.update(available_memory_bytes=memory['available_memory_bytes'], free_disk_bytes=free,
                     disk_path=str(disk_path.resolve()))
        if free < float(config.get("min_free_disk_gib", 30)) * 2**30:
            errors.append("insufficient free disk")
        sched_path = Path('/sys/kernel/sched_ext/state')
        sched = _text(sched_path)
        state["sched_ext"] = sched if sched_path.exists() else "absent"
        if sched_path.exists() and sched != 'disabled':
            errors.append("sched_ext is active or unreadable")
        state["load_average"] = list(os.getloadavg())
        if state['load_average'][0] > float(config.get('max_load_average', max(2, len(allowed) / 2))):
            errors.append('excessive concurrent system load')
        state["top_cpu_processes"] = _processes()
        if any(r['cpu_percent'] >= float(config.get('heavy_process_cpu_percent', 50))
               and r['pid'] not in config.get('expected_workload_pids', [])
               for r in state['top_cpu_processes']):
            errors.append("concurrent heavy CPU process")
        state['throttle_counters'] = {
            str(p): _text(p) for p in Path('/sys/devices/system/cpu').glob('cpu*/thermal_throttle/*') if p.is_file()}
        if expected is not None:
            if allowed != expected.get('allowed_cpus'):
                errors.append('allowed CPU affinity changed')
            for field in POWER_FIELDS:
                if power.get(field) != expected.get('power', {}).get(field):
                    errors.append(f'power state changed: {field}')
    except (OSError, ValueError, c03.PreflightError) as exc:
        errors.append(str(exc))
    state['status'] = 'NOT_READY' if errors else 'PASS'
    return state


def _apply_frozen_patch(original, patch):
    """Apply complete, exact hunks, allowing only a unique +/-3 line offset."""
    lines, output, cursor = original.splitlines(keepends=True), [], 0
    patch_lines = patch.splitlines(keepends=True)
    i, hunks = 0, 0
    while i < len(patch_lines):
        match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*\n?', patch_lines[i])
        if not match:
            if hunks or patch_lines[i].startswith('@@') or not patch_lines[i].startswith(
                    ('diff --git ', 'index ', '--- ', '+++ ')):
                raise ValueError('malformed frozen patch')
            i += 1
            continue
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        start = int(match.group(1)) - (1 if old_count else 0)
        if start < 0 or not old_count:
            raise ValueError('frozen patch requires original context')
        before, after = [], []
        i += 1
        while i < len(patch_lines) and patch_lines[i][:1] in (' ', '+', '-'):
            line = patch_lines[i]; kind, content = line[0], line[1:]
            if kind in (' ', '-'):
                before.append(content)
            if kind in (' ', '+'):
                after.append(content)
            i += 1
        if len(before) != old_count or len(after) != new_count:
            raise ValueError('frozen patch hunk count mismatch')
        candidates = [pos for pos in range(max(cursor, start - 3),
                                           min(len(lines) - old_count, start + 3) + 1)
                      if lines[pos:pos + old_count] == before]
        if not candidates:
            raise ValueError('frozen patch context mismatch or overlapping hunks')
        if len(candidates) != 1:
            raise ValueError('ambiguous frozen patch context')
        position = candidates[0]
        output.extend(lines[cursor:position])
        output.extend(after)
        cursor = position + old_count
        hunks += 1
    if not hunks:
        raise ValueError('frozen patch has no hunks')
    output.extend(lines[cursor:])
    return ''.join(output)


def _validate_emitted_flags(cache, build):
    """Inspect actual compiler command lines, not just GGML option labels."""
    flags = {key: value for key, value in cache.items()
             if re.match(r'CMAKE_(?:C|CXX)_FLAGS(?:_|$)', key)}
    command_path = build / 'compile_commands.json'
    texts = list(flags.values())
    evidence = []
    if command_path.is_file():
        commands = json.loads(command_path.read_text())
        if not isinstance(commands, list) or not commands:
            raise ValueError('emitted ISA provenance gap: compile commands empty')
        for entry in commands:
            arguments = entry.get('arguments')
            command = ' '.join(arguments) if arguments else entry.get('command', '')
            if not command:
                raise ValueError('emitted ISA provenance gap: compiler command missing')
            texts.append(command)
        evidence.append({'kind': 'compile_commands', 'identity': identity(command_path)})
    elif (build / 'build.ninja').is_file():
        # -t commands only prints generated commands; it never invokes the build.
        result = c03.run_readonly(['ninja', '-C', str(build), '-t', 'commands'])
        if result['returncode'] != 0 or not result['stdout'].strip():
            raise ValueError('emitted ISA provenance gap: ninja commands unreadable')
        texts.extend(result['stdout'].splitlines())
        ninja_files = sorted({build / 'build.ninja', *build.glob('CMakeFiles/*.ninja')})
        evidence.append({'kind': 'ninja_tool_commands', 'files': [identity(v) for v in ninja_files],
                         'command': result['command'],
                         'commands_sha256': hashlib.sha256(result['stdout'].encode()).hexdigest()})
    else:
        make_flags = sorted(build.glob('**/CMakeFiles/*.dir/flags.make'))
        if not make_flags:
            raise ValueError('emitted ISA provenance gap: no compile commands, Ninja, or flags.make metadata')
        for path in make_flags:
            text = path.read_text()
            if not re.search(r'^(?:C|CXX)_FLAGS\s*=', text, re.M):
                raise ValueError(f'emitted ISA provenance gap: generated flags missing in {path}')
            texts.append(text)
        evidence.append({'kind': 'cmake_make_flags', 'files': [identity(v) for v in make_flags]})
    # CPU-specific -march/-mcpu can silently exceed the historical AVX2 baseline.
    for text in texts:
        if re.search(r'(?:-march|-mcpu|-mtune)=native\b|-mavx512\w*\b|/arch:AVX512', text, re.I):
            raise ValueError('emitted ISA policy mismatch: native or AVX512 compiler flags')
        for architecture in re.findall(r'-march=([^\s"\x27]+)', text):
            if architecture not in ('x86-64', 'x86-64-v2', 'x86-64-v3'):
                raise ValueError(f'emitted ISA policy cannot establish AVX2 baseline: -march={architecture}')
    return {'cache_flags': flags, 'evidence': evidence, 'inspected_flags_and_commands': texts}


def _runtime_closure(binary):
    """Ask the ELF loader to list dependencies, never start the server/model.

    Call only after the server hash has been anchored to approved evidence.
    The loader resolves transitive DT_NEEDED entries including system runtimes.
    """
    if any(os.environ.get(key) for key in ('LD_PRELOAD', 'LD_AUDIT')):
        raise ValueError('RUNTIME_LOADER_OVERRIDE: injected loader hooks require author review; no listing/execution attempted')
    if shutil.which('readelf') is None:
        raise ValueError('READONLY_TOOL_MISSING: readelf; no installation attempted')
    header = c03.run_readonly(['readelf', '-lW', str(binary)])
    match = re.search(r'Requesting program interpreter:\s*([^\]]+)\]', header.get('stdout', ''))
    if header.get('returncode') != 0 or not match:
        raise ValueError('ORIGINAL_BINARY_INCOMPATIBLE: ELF interpreter unavailable; author-reviewed fallback required')
    loader = Path(match.group(1)).resolve()
    if not loader.is_file() or not os.access(loader, os.X_OK):
        raise ValueError('ORIGINAL_BINARY_INCOMPATIBLE: ELF loader unavailable; author-reviewed fallback required')
    result = c03.run_readonly([str(loader), '--list', str(binary)])
    output = result.get('stdout', '')
    if result.get('returncode') != 0 or 'not found' in output:
        raise ValueError('ORIGINAL_BINARY_INCOMPATIBLE: runtime dependency resolution failed; author-reviewed fallback required')
    paths = {loader}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(('linux-vdso', 'linux-gate')):
            continue
        target = line.split('=>', 1)[-1].strip().split(' (', 1)[0]
        if not target.startswith('/') or not Path(target).is_file():
            raise ValueError('runtime dependency resolution incomplete: ' + line)
        paths.add(Path(target).resolve())
    if len(paths) < 2:
        raise ValueError('runtime dependency resolution empty')
    # Loader addresses vary with ASLR; they are not file identity or resolution.
    result = dict(result)
    result['stdout'] = re.sub(r'\(0x[0-9a-fA-F]+\)', '(address omitted)', output)
    return [identity(path) for path in sorted(paths)], {
        'interpreter': identity(loader), 'readelf': header, 'loader_list': result,
        'loader_environment': {key: value for key, value in sorted(os.environ.items())
                               if key.startswith('LD_') or key == 'GLIBC_TUNABLES'}}


def verify_build(config):
    if config.get('runtime_fallback_manifest'):
        from .runtime_fallback import verify_fallback
        return verify_fallback(config)
    root = Path(config['repo_root']).resolve(); source = root / 'llama.cpp'
    binary = Path(config['server_bin']).resolve(); build = binary.parent.parent
    if not binary.is_file():
        raise ValueError('ORIGINAL_BINARY_MISSING: author-reviewed fallback required')
    pinned = config.get('pinned_revision', c03.PINNED_LLAMA_COMMIT)
    if pinned != c03.PINNED_LLAMA_COMMIT:
        raise ValueError('source pin substitution forbidden')
    if c03.git_output(['rev-parse', 'HEAD'], cwd=source) != pinned:
        raise ValueError('llama.cpp source revision mismatch')
    patch_path = root / 'conference/diagnostic/llama_cpp_phase_mark.patch'
    patch_bytes = patch_path.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != PHASE_PATCH_SHA256:
        raise ValueError('historical phase patch SHA256 mismatch')
    original = c03.git_output(['show', f'{pinned}:src/llama-context.cpp'], cwd=source) + '\n'
    reconstructed = _apply_frozen_patch(original, patch_bytes.decode())
    if c03.MARKER.decode() not in reconstructed:
        raise ValueError('reconstructed source lacks historical PHASE_MARK format')
    changed = c03.git_output(['diff', 'HEAD', '--name-only'], cwd=source).splitlines()
    if changed not in ([], ['src/llama-context.cpp']):
        raise ValueError('source changes exceed the frozen C03 phase patch')
    if c03.git_output(['ls-files', '--others', '--exclude-standard'], cwd=source):
        raise ValueError('nonignored untracked source inputs forbidden')
    context_bytes = (source / 'src/llama-context.cpp').read_bytes()
    if not changed and context_bytes == original.encode():
        source_state = 'clean_pinned_base_binary_separately_anchored'
    elif changed == ['src/llama-context.cpp'] and context_bytes == reconstructed.encode():
        source_state = 'exact_frozen_phase_patch'
    else:
        raise ValueError('source differs from clean pinned base or reconstructed historical C03 phase patch')
    # This records source state only. Existing binary/library identity checks
    # below separately anchor the executable; no build equivalence is inferred.
    reconstructed_sha256 = hashlib.sha256(reconstructed.encode()).hexdigest()
    cache_path = build / 'CMakeCache.txt'; cache = c03.parse_cmake_cache(cache_path)
    if (cache.get('GGML_BACKEND_DL') != 'OFF' or cache.get('GGML_CPU_ALL_VARIANTS') != 'OFF'
            or cache.get('GGML_BACKEND_DIR') != '' or os.environ.get('GGML_BACKEND_PATH')):
        raise ValueError('DYNAMIC_BACKEND_PROVENANCE_GAP: explicit existing cache proof of disabled backend loading and empty backend directory required; GGML_BACKEND_PATH must be unset or empty')
    required = {'CMAKE_BUILD_TYPE': 'Release', 'GGML_NATIVE': 'OFF',
                'GGML_AVX': 'ON', 'GGML_AVX2': 'ON', 'GGML_FMA': 'ON',
                'GGML_F16C': 'ON', 'GGML_AVX512': 'OFF', 'GGML_OPENMP': 'ON',
                'GGML_OPENMP_ENABLED': 'ON', 'BUILD_SHARED_LIBS': 'ON'}
    for key, value in required.items():
        if cache.get(key) != value:
            raise ValueError(f'C03 AVX2 build policy mismatch: {key}')
    if any(v != 'OFF' for k, v in cache.items() if k.startswith('GGML_AVX512')):
        raise ValueError('AVX512 build option enabled')
    if Path(cache.get('CMAKE_HOME_DIRECTORY', '')).resolve() != source:
        raise ValueError('CMake source directory mismatch')
    emitted_flags = _validate_emitted_flags(cache, build)
    binary_id = identity(binary)
    if binary_id['sha256'] != ORIGINAL_SERVER_SHA256:
        raise ValueError('ORIGINAL_BINARY_MISMATCH: author-reviewed fallback required; build manifest cannot authorize substitution')
    supplied_historical = config.get('historical_identity')
    if supplied_historical:
        supplied_historical = supplied_historical.get('diagnostic_build', supplied_historical)
        if supplied_historical.get('llama_cpp_commit', pinned) != pinned:
            raise ValueError('historical source commit mismatch')
        if supplied_historical.get('patch_sha256', PHASE_PATCH_SHA256) != PHASE_PATCH_SHA256:
            raise ValueError('historical phase patch SHA256 mismatch')
        if supplied_historical.get('server_binary', {}).get('sha256') != ORIGINAL_SERVER_SHA256:
            raise ValueError('ORIGINAL_BINARY_MISMATCH: supplied historical anchor differs; author-reviewed fallback required')
    manifest_path = config.get('build_manifest')
    approved_hash = config.get('expected_build_manifest_sha256')
    if not manifest_path:
        historical_path = Path(config.get('historical_preflight', root / 'results/conference_c03/preflight/selected_topology.json'))
        if not config.get('historical_identity') and not historical_path.is_file():
            raise ValueError('existing historical C03 identity metadata or approved build manifest required; neither is available')
        historical = config.get('historical_identity') or json.loads(historical_path.read_text())
        historical = historical.get('diagnostic_build', historical)
        if historical.get('llama_cpp_commit', pinned) != pinned:
            raise ValueError('historical source commit mismatch')
        if historical.get('patch_sha256', PHASE_PATCH_SHA256) != PHASE_PATCH_SHA256:
            raise ValueError('historical phase patch SHA256 mismatch')
        compiler_identity = historical.get('compiler_identity')
        if (not compiler_identity or
                (isinstance(compiler_identity, dict) and
                 (compiler_identity.get('returncode') != 0 or not compiler_identity.get('stdout', '').strip()))):
            raise ValueError('historical C03 compiler identity missing')
        if not binary.is_file():
            raise ValueError('ORIGINAL_BINARY_MISSING: author-reviewed fallback required')
        binary_id = identity(binary)
        if (historical.get('server_binary', {}).get('sha256') != ORIGINAL_SERVER_SHA256
                or binary_id['sha256'] != ORIGINAL_SERVER_SHA256):
            raise ValueError('ORIGINAL_BINARY_MISMATCH: author-reviewed fallback required')
        if not os.access(binary, os.X_OK):
            raise ValueError('ORIGINAL_BINARY_INCOMPATIBLE: not executable; author-reviewed fallback required')
        library_ids, resolution = _runtime_closure(binary)
        recorded = historical.get('phase_mark_capability', {}).get('matched_file_identities', [])
        current_paths = [binary, *(Path(v['resolved_path']) for v in library_ids)]
        matches = [identity(path) for path in current_paths if c03.MARKER in path.read_bytes()]
        # Historical evidence is partial: retain every known anchor without
        # pretending it established the identity of all transitive runtimes.
        known_marker_hashes = {entry['sha256'] for entry in recorded}
        current_marker_library_hashes = {entry['sha256'] for entry in matches
                                        if entry['resolved_path'] != str(binary)}
        if not known_marker_hashes <= current_marker_library_hashes:
            raise ValueError('ORIGINAL_KNOWN_LIBRARY_MISMATCH: author-reviewed fallback required')
        if not matches or any(b'C04_WORKER_TAIL' in path.read_bytes() for path in current_paths):
            raise ValueError('ORIGINAL_BINARY_INCOMPATIBLE: static PHASE_MARK capability missing or C04 worker tail present; author-reviewed fallback required')
        capability = {'supported': True, 'method': 'static_bytes_runtime_closure',
                      'matched_file_identities': matches}
        compiler_ids = {}
        for key in ('CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER'):
            if cache.get(key):
                if not Path(cache[key]).is_file():
                    raise ValueError(f'current compiler identity unavailable: {key}')
                compiler_ids[key] = identity(cache[key])
        if 'CMAKE_CXX_COMPILER' not in compiler_ids:
            raise ValueError('current compiler identity unavailable')
        return {'binary': binary_id, 'shared_libraries': library_ids,
                'runtime_resolution': resolution, 'compiler_file_identity': compiler_ids,
                'historical_known_identities': {'server_binary': historical['server_binary'],
                                                'marker_libraries': recorded},
                'phase_mark': capability, 'source_commit': pinned,
                'source_state': source_state, 'reconstructed_source_sha256': reconstructed_sha256,
                'binary_source_equivalence': False,
                'source_files': {n: identity(source / n) for n in c03.git_output(['ls-files'], cwd=source).splitlines()},
                'phase_patch': identity(patch_path), 'cmake_cache': identity(cache_path),
                'historical_compiler_identity': historical.get('compiler_identity'),
                'historical_preflight': identity(historical_path) if historical_path.is_file() else None,
                'emitted_flags': emitted_flags,
                'provenance_mode': 'prospective_current_identity_with_historical_anchor',
                'full_build_manifest_verified': False}
    if not approved_hash:
        raise ValueError('existing build manifest requires author-approved SHA256')
    manifest_id = identity(manifest_path)
    if manifest_id['sha256'] != approved_hash:
        raise ValueError('approved build manifest SHA256 mismatch')
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get('source_commit') != pinned or not manifest.get('compiler_identity'):
        raise ValueError('manifest source/compiler provenance missing')
    tracked = c03.git_output(['ls-files'], cwd=source).splitlines()
    hashes = manifest.get('source_file_sha256', {})
    if not tracked or set(tracked) != set(hashes):
        raise ValueError('manifest does not cover the complete tracked source tree')
    source_ids = {}
    for name in tracked:
        path = source / name
        if not path.resolve().is_relative_to(source):
            raise ValueError('source path escapes source tree')
        source_ids[name] = identity(path)
        if source_ids[name]['sha256'] != hashes[name]:
            raise ValueError(f'manifest source hash mismatch: {name}')
    binary_id = identity(binary)
    if not os.access(binary, os.X_OK) or binary_id['sha256'] != manifest.get('binary', {}).get('sha256'):
        raise ValueError('manifest binary mismatch or not executable')
    library_ids, resolution = _runtime_closure(binary)
    libraries = [Path(v['resolved_path']) for v in library_ids]
    if supplied_historical:
        known = {entry['sha256'] for entry in supplied_historical.get(
            'phase_mark_capability', {}).get('matched_file_identities', [])}
        current_markers = {entry['sha256'] for entry in library_ids
                           if c03.MARKER in Path(entry['resolved_path']).read_bytes()}
        if not known <= current_markers:
            raise ValueError('ORIGINAL_KNOWN_LIBRARY_MISMATCH: author-reviewed fallback required; build manifest cannot waive historical anchors')
    approved_libraries = {Path(v['path']).resolve(): v['sha256'] for v in manifest.get('shared_libraries', [])}
    if not libraries or set(libraries) != set(approved_libraries):
        raise ValueError('manifest shared-library coverage mismatch')
    if any(v['sha256'] != approved_libraries[Path(v['resolved_path'])] for v in library_ids):
        raise ValueError('manifest shared-library hash mismatch')
    capability = c03.scan_phase_mark_capability(binary)
    if any(b'C04_WORKER_TAIL' in p.read_bytes() for p in [binary, *libraries]):
        raise ValueError('C04 worker-tail binary is not historical C03 build')
    compiler = Path(cache.get('CMAKE_CXX_COMPILER', ''))
    return {'binary': binary_id, 'shared_libraries': library_ids,
            'runtime_resolution': resolution,
            'source_files': source_ids, 'phase_patch': identity(patch_path),
            'cmake_cache': identity(cache_path), 'build_manifest': manifest_id,
            'compiler': identity(compiler), 'compiler_identity': manifest['compiler_identity'],
            'emitted_flags': emitted_flags,
            'phase_mark': capability, 'source_commit': pinned,
            'source_state': source_state, 'reconstructed_source_sha256': reconstructed_sha256}


def perform_preflight(config, destination):
    """Write a new exclusive report directory; never alter historical results."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    errors, warnings = [], []
    report = dict(status='NOT_READY', errors=errors, warnings=warnings,
                  topology={}, identities={}, machine_state={}, source_fingerprint={})
    report['required_tools'] = {name: shutil.which(name) for name in ('taskset', 'lscpu', 'ps', 'git', 'readelf')}
    for name, executable in report['required_tools'].items():
        if executable is None:
            errors.append(f'required read-only tool unavailable: {name}')
    try:
        allowed = sorted(os.sched_getaffinity(0)); records = c03.collect_cpu_records(allowed=allowed)
        cores = c03.group_physical_cores(records); selection = c03.classify_physical_cores(cores)
        c03.validate_target(c03.cpu_identity(), records, cores, selection)
        big = [v['representative_cpu'] for v in selection['big_cores']]
        compact = sorted(selection['compact_cores'], key=lambda v: (v['package_id'], v['core_id']))
        compact_all = [v['representative_cpu'] for v in compact]
        c03.validate_masks(big, compact_all, cores)
        representatives = big + compact_all
        online = {r['cpu'] for r in records if r.get('online')}
        if not set(representatives).issubset(online & set(allowed)):
            raise ValueError('selected mask CPU is offline or outside invoking allowed affinity')
        report['topology'] = dict(cpu_identity=c03.cpu_identity(), allowed_cpus=allowed,
            logical_cpus=records, physical_cores=cores, classification=selection,
            masks={'BIG4': big, 'COMPACT4': compact_all[:4],
                   'ALL_ALLOWED4': sorted(big + compact_all), 'ALL_CORES12': sorted(big + compact_all)})
    except (OSError, ValueError, c03.PreflightError) as exc:
        errors.append(f'topology: {exc}')
    for name, default in [('model', MODEL_SHA256), ('prompt', PROMPT_SHA256)]:
        try:
            entry = identity(config[name]); report['identities'][name] = entry
            if config.get(f'expected_{name}_sha256', default) != default or entry['sha256'] != default:
                raise ValueError(f'{name} frozen SHA256 mismatch')
        except (OSError, KeyError, ValueError) as exc:
            errors.append(str(exc))
    try:
        fingerprint = verify_build(config)
        report['source_fingerprint'] = fingerprint
        report['identities']['server'] = fingerprint['binary']
    except (OSError, KeyError, ValueError, c03.PreflightError) as exc:
        errors.append(f'build provenance: {exc}')
    report['machine_state'] = check_machine_state(config)
    errors.extend(report['machine_state']['errors']); warnings.extend(report['machine_state']['warnings'])
    try:
        root = Path(config['repo_root'])
        report['git'] = {name: c03.git_output(args, cwd=root) for name, args in {
            'commit': ['rev-parse', 'HEAD'], 'branch': ['branch', '--show-current'],
            'dirty_porcelain': ['status', '--porcelain', '--untracked-files=all']}.items()}
        report['environment'] = {'lscpu': c03.run_readonly(['lscpu']),
                                 'uname': c03.run_readonly(['uname', '-a'])}
    except (OSError, ValueError, c03.PreflightError) as exc:
        errors.append(f'environment: {exc}')
    report['status'] = 'NOT_READY' if errors else 'PASS'
    report['captured_at'] = c03.utc_timestamp()
    for name, value in [('preflight.json', report), ('topology.json', report['topology']),
                        ('identities.json', report['identities']), ('environment.json', report['machine_state'])]:
        with (destination / name).open('x', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, sort_keys=True); handle.write('\n')
    return report
