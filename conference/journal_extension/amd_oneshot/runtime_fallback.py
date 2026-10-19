"""Explicitly approved, offline prospective runtime; never historical identity.

Importing or verifying this module does not build or start an inference server.
Only build_fallback performs the separately authorized local compilation.
"""
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import time

from .common import digest, save_json, sha256_file
from . import preflight


OPTIONS = {
    'CMAKE_BUILD_TYPE': 'Release', 'CMAKE_EXPORT_COMPILE_COMMANDS': 'ON',
    'GGML_NATIVE': 'OFF', 'GGML_AVX': 'ON', 'GGML_AVX2': 'ON',
    'GGML_FMA': 'ON', 'GGML_F16C': 'ON', 'GGML_AVX512': 'OFF',
    'GGML_AVX512_VBMI': 'OFF', 'GGML_AVX512_VNNI': 'OFF', 'GGML_AVX512_BF16': 'OFF',
    'GGML_AVX_VNNI': 'OFF', 'GGML_AMX_TILE': 'OFF', 'GGML_AMX_INT8': 'OFF', 'GGML_AMX_BF16': 'OFF',
    'GGML_CCACHE': 'OFF', 'GGML_CPU_REPACK': 'ON', 'GGML_CPU_KLEIDIAI': 'OFF',
    'GGML_BACKEND_DL': 'OFF', 'GGML_CPU_ALL_VARIANTS': 'OFF',
    'GGML_BACKEND_DIR': '', 'GGML_OPENMP': 'ON', 'BUILD_SHARED_LIBS': 'ON',
    'GGML_CUDA': 'OFF', 'GGML_VULKAN': 'OFF', 'GGML_HIP': 'OFF',
    'GGML_SYCL': 'OFF', 'GGML_METAL': 'OFF', 'GGML_RPC': 'OFF',
    'GGML_ZENDNN': 'OFF', 'GGML_VIRTGPU': 'OFF', 'GGML_HEXAGON': 'OFF',
    'LLAMA_BUILD_TESTS': 'OFF', 'LLAMA_BUILD_EXAMPLES': 'OFF',
    'LLAMA_BUILD_SERVER': 'ON', 'LLAMA_BUILD_TOOLS': 'ON', 'LLAMA_BUILD_APP': 'OFF',
    'LLAMA_BUILD_UI': 'OFF', 'LLAMA_USE_PREBUILT_UI': 'OFF',
    'LLAMA_BUILD_MTMD': 'OFF', 'LLAMA_TOOLS_INSTALL': 'OFF',
    'LLAMA_LLGUIDANCE': 'OFF',
    'LLAMA_BUILD_BORINGSSL': 'OFF', 'LLAMA_BUILD_LIBRESSL': 'OFF',
    'FETCHCONTENT_FULLY_DISCONNECTED': 'ON', 'FETCHCONTENT_UPDATES_DISCONNECTED': 'ON',
}
POLICY = {'schema': 'je-amd-offline-runtime-fallback-v1',
          'source_revision': preflight.c03.PINNED_LLAMA_COMMIT,
          'phase_patch': 'conference/diagnostic/llama_cpp_phase_mark.patch',
          'phase_patch_sha256': '722adc5c8d8805d442753971d2eabd488ab91906aa8fa93961d6a615194833ce',
          'options': OPTIONS, 'maximum_build_s': 900, 'maximum_session_s': 10800,
          'parallel_jobs': 4, 'network': 'forbidden', 'historical_equivalence': False}
POLICY_SHA256 = digest(POLICY)


def validate_approval(config, approval):
    from . import protocol
    git_report = None
    if config.get('identity_kind') == 'git':
        from . import git_delivery
        git_report = git_delivery.verify_checkout(config['repo_root'], expected_commit=config['git_commit'])
        if approval.get('schema') != 'je-amd-git-approval-v1':
            raise ValueError('RUNTIME_FALLBACK_UNAPPROVED: stale ZIP-era approval')
    protocol.check_approval(config['plan'], approval, config.get('package_sha256'), git_report=git_report)
    if (approval.get('authorized') is not True or approval.get('runtime_fallback_authorized') is not True
            or not approval.get('author_review_id')
            or approval.get('runtime_fallback_policy_sha256') != POLICY_SHA256
            or approval.get('plan_sha256') != digest(config['plan'])
            or approval.get('source_sha256' if git_report is not None else 'package_sha256') != config.get('package_sha256')
            or not config.get('package_sha256')):
        raise ValueError('RUNTIME_FALLBACK_UNAPPROVED: immutable author approval/source/plan/policy binding required')


def _run(command, *, cwd, timeout, log_path, env):
    """Bound the entire process group, including descendants on cancellation."""
    with Path(log_path).open('xb') as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            finally:
                # A reaped parent does not prove its descendants have exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise
    if code:
        raise ValueError(f'RUNTIME_FALLBACK_BUILD_FAILED: command exit {code}; see {log_path}')


def _source_ids(source):
    paths = sorted(source.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise ValueError('RUNTIME_FALLBACK_SOURCE: symbolic links forbidden')
    return {str(path.relative_to(source)): preflight.identity(path)
            for path in paths if path.is_file()}


def _fingerprint(source, build, patch):
    binary = build / 'bin/llama-server'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError('RUNTIME_FALLBACK_INVALID: executable server missing')
    cache_path = build / 'CMakeCache.txt'
    cache = preflight.c03.parse_cmake_cache(cache_path)
    for key, value in {**OPTIONS, 'GGML_OPENMP_ENABLED': 'ON'}.items():
        if cache.get(key) != value:
            raise ValueError(f'RUNTIME_FALLBACK_POLICY_MISMATCH: {key}')
    if any(value != 'OFF' for key, value in cache.items() if key.startswith('GGML_AVX512')):
        raise ValueError('RUNTIME_FALLBACK_POLICY_MISMATCH: AVX512')
    if Path(cache.get('CMAKE_HOME_DIRECTORY', '')).resolve() != source.resolve():
        raise ValueError('RUNTIME_FALLBACK_SOURCE: CMake directory mismatch')
    if os.environ.get('GGML_BACKEND_PATH'):
        raise ValueError('DYNAMIC_BACKEND_PROVENANCE_GAP: GGML_BACKEND_PATH')
    emitted = preflight._validate_emitted_flags(cache, build)
    libraries, resolution = preflight._runtime_closure(binary)
    project_libraries = [Path(entry['resolved_path']) for entry in libraries
                         if Path(entry['resolved_path']).name.startswith(('libllama.', 'libggml'))]
    if (not any(path.name.startswith('libllama.') for path in project_libraries)
            or any(not path.is_relative_to(build.resolve()) for path in project_libraries)):
        raise ValueError('RUNTIME_FALLBACK_INVALID: project libraries must resolve to the new isolated build')
    candidates = [binary, *(Path(v['resolved_path']) for v in libraries)]
    matches = [preflight.identity(path) for path in candidates if preflight.c03.MARKER in path.read_bytes()]
    if not matches or any(b'C04_WORKER_TAIL' in path.read_bytes() for path in candidates):
        raise ValueError('RUNTIME_FALLBACK_INVALID: frozen PHASE_MARK missing or C04 instrumentation present')
    compilers = {}
    for key in ('CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER'):
        if not cache.get(key) or not Path(cache[key]).is_file():
            raise ValueError('RUNTIME_FALLBACK_INVALID: compiler file identity missing')
        compilers[key] = preflight.identity(cache[key])
    return {'binary': preflight.identity(binary), 'shared_libraries': libraries,
            'source_files': _source_ids(source), 'cmake_cache': preflight.identity(cache_path),
            'phase_patch': preflight.identity(patch), 'compiler_file_identity': compilers,
            'emitted_flags': emitted, 'runtime_resolution': resolution,
            'phase_mark': {'supported': True, 'method': 'static_bytes_runtime_closure',
                           'matched_file_identities': matches},
            'source_commit': POLICY['source_revision'],
            'provenance_mode': 'new_prospective_author_approved_runtime_fallback',
            'historical_binary_equivalent': False}


def build_fallback(config, approval, work_root, session_started_wall_s, *, clock=time.time,
                   monotonic=time.monotonic, runner=None):
    """Create a new isolated local runtime after separate immutable approval.

    Caller must run normal machine/model checks before and after this operation.
    A failed workspace and logs are retained for the private failure archive.
    """
    validate_approval(config, approval)
    if any(os.environ.get(key) for key in ('GGML_BACKEND_PATH', 'LD_PRELOAD', 'LD_AUDIT')):
        raise ValueError('RUNTIME_FALLBACK_ENVIRONMENT: unapproved runtime injection')
    repo = Path(config['repo_root']).resolve()
    local_source = repo / 'llama.cpp'
    work = Path(work_root).resolve()
    if (work == repo or work.is_relative_to(local_source.resolve())
            or work.is_relative_to(repo / 'results/conference_c03')
            or repo.is_relative_to(work)):
        raise ValueError('RUNTIME_FALLBACK_ISOLATION: new workspace must be outside historical source/build/results')
    reserve = float(config.get('archive_reserve_s', config.get('plan', {}).get('archive_reserve_s', 180)))
    started = clock()
    mono_started = monotonic()
    if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in (started, mono_started, session_started_wall_s, reserve))
            or session_started_wall_s < 0 or session_started_wall_s > started
            or reserve < 0 or reserve >= POLICY['maximum_session_s']):
        raise ValueError('RUNTIME_FALLBACK_INVALID_START_TIME')
    deadline = min(started + POLICY['maximum_build_s'],
                   float(session_started_wall_s) + POLICY['maximum_session_s'] - reserve)
    if not deadline > started:
        raise ValueError('RUNTIME_FALLBACK_BUDGET_EXHAUSTED')
    tools = {key: shutil.which(name) for key, name in (
        ('git', 'git'), ('cmake', 'cmake'), ('make', 'make'), ('cc', 'cc'), ('cxx', 'c++'))}
    if any(not value for value in tools.values()):
        raise ValueError('RUNTIME_FALLBACK_PREREQUISITE_MISSING: existing git/cmake/make/C/C++ compilers required; no installation')
    if preflight.c03.git_output(['rev-parse', POLICY['source_revision'] + '^{commit}'], cwd=local_source) != POLICY['source_revision']:
        raise ValueError('RUNTIME_FALLBACK_SOURCE: pinned local commit unavailable')
    work.mkdir(parents=True, exist_ok=False)
    source, build = work / 'source', work / 'build'
    source.mkdir()
    patch = repo / POLICY['phase_patch']
    if sha256_file(patch) != POLICY['phase_patch_sha256']:
        raise ValueError('RUNTIME_FALLBACK_SOURCE: frozen phase patch hash mismatch')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('CMAKE_') and key not in ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'CMAKE_PREFIX_PATH',
                          'CMAKE_TOOLCHAIN_FILE', 'CC', 'CXX', 'MAKEFLAGS')}
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    commands = []
    execute = runner or _run
    last_wall, last_mono = started, mono_started
    def remaining_budget():
        nonlocal last_wall, last_mono
        wall, mono = clock(), monotonic()
        if not math.isfinite(wall) or not math.isfinite(mono) or wall < last_wall or mono < last_mono:
            raise ValueError('RUNTIME_FALLBACK_CLOCK_ROLLBACK')
        last_wall, last_mono = wall, mono
        remaining = min(deadline - wall, mono_started + POLICY['maximum_build_s'] - mono)
        if remaining <= 0:
            raise ValueError('RUNTIME_FALLBACK_BUDGET_EXHAUSTED')
        return remaining
    def run(command, cwd=work):
        remaining = remaining_budget()
        log = work / f'command-{len(commands):02d}.log'
        commands.append({'command': command, 'cwd': str(cwd), 'log': str(log)})
        execute(command, cwd=cwd, timeout=remaining, log_path=log, env=env)
    try:
        archive = work / 'pinned-source.tar'
        run([tools['git'], 'archive', '--format=tar', '--output=' + str(archive), POLICY['source_revision']], local_source)
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if not (member.isfile() or member.isdir()) or not (source / member.name).resolve().is_relative_to(source):
                    raise ValueError('RUNTIME_FALLBACK_SOURCE: unsafe archive member')
            bundle.extractall(source, filter='data')
        context = source / 'src/llama-context.cpp'
        original = context.read_text()
        context.write_text(preflight._apply_frozen_patch(original, patch.read_text()))
        if preflight.c03.MARKER not in context.read_bytes():
            raise ValueError('RUNTIME_FALLBACK_SOURCE: frozen marker patch missing')
        configure = [tools['cmake'], '-S', str(source), '-B', str(build), '-G', 'Unix Makefiles',
                     '-DCMAKE_C_COMPILER=' + tools['cc'], '-DCMAKE_CXX_COMPILER=' + tools['cxx'],
                     '-DCMAKE_MAKE_PROGRAM=' + tools['make']]
        configure += [f'-D{key}={value}' for key, value in sorted(OPTIONS.items())]
        run(configure)
        run([tools['cmake'], '--build', str(build), '--target', 'llama-server', '--parallel', '4'])
        for name in ('cc', 'cxx'):
            run([tools[name], '--version'])
        remaining_budget()
        fingerprint = _fingerprint(source, build, patch)
        remaining_budget()
        record = {'schema': POLICY['schema'], 'status': 'BUILT', 'policy_sha256': POLICY_SHA256,
                  'approval_sha256': digest(approval), 'plan_sha256': digest(config['plan']),
                  'package_sha256': config['package_sha256'], 'source': str(source), 'build': str(build),
                  'source_archive': preflight.identity(archive), 'fingerprint': fingerprint,
                  'commands': commands, 'logs': [preflight.identity(item['log']) for item in commands],
                  'compiler_version_capture': {
                      key: Path(commands[index]['log']).read_text(errors='replace')
                      for key, index in (('C', -2), ('CXX', -1))},
                  'elapsed_s': clock() - started, 'session_started_wall_s': session_started_wall_s}
        record_path = work / 'runtime-fallback-manifest.json'
        save_json(record_path, record)
        overrides = {'server_bin': fingerprint['binary']['resolved_path'],
                     'runtime_fallback_manifest': str(record_path),
                     'expected_runtime_fallback_manifest_sha256': sha256_file(record_path),
                     'runtime_fallback_approval': approval,
                     'expected_runtime_fallback_approval_sha256': digest(approval)}
        return {'config_overrides': overrides, 'manifest': record}
    except BaseException as exc:
        save_json(work / 'runtime-fallback-failure.json', {
            'status': 'FAILED', 'type': type(exc).__name__, 'error': str(exc),
            'commands': commands, 'elapsed_s': clock() - started,
            'approval_sha256': digest(approval), 'policy_sha256': POLICY_SHA256})
        raise


def verify_fallback(config):
    """Recheck approved build record and every current prospective identity."""
    approval = config.get('runtime_fallback_approval', {})
    validate_approval(config, approval)
    if config.get('expected_runtime_fallback_approval_sha256') != digest(approval):
        raise ValueError('RUNTIME_FALLBACK_APPROVAL_MISMATCH')
    manifest_path = Path(config['runtime_fallback_manifest'])
    if sha256_file(manifest_path) != config.get('expected_runtime_fallback_manifest_sha256'):
        raise ValueError('RUNTIME_FALLBACK_MANIFEST_MISMATCH')
    record = json.loads(manifest_path.read_text())
    if sha256_file(Path(config['repo_root']) / POLICY['phase_patch']) != POLICY['phase_patch_sha256']:
        raise ValueError('RUNTIME_FALLBACK_SOURCE: frozen phase patch hash mismatch')
    if (record.get('status') != 'BUILT' or record.get('policy_sha256') != POLICY_SHA256
            or record.get('approval_sha256') != digest(approval)
            or record.get('plan_sha256') != digest(config['plan'])
            or record.get('package_sha256') != config['package_sha256']):
        raise ValueError('RUNTIME_FALLBACK_BUILD_RECORD_MISMATCH')
    if Path(config['server_bin']).resolve() != Path(record['fingerprint']['binary']['resolved_path']):
        raise ValueError('RUNTIME_FALLBACK_BINARY_SUBSTITUTION')
    for old in [record['source_archive'], *record['logs']]:
        if preflight.identity(old['resolved_path']) != old:
            raise ValueError('RUNTIME_FALLBACK_BUILD_EVIDENCE_DRIFT')
    current = _fingerprint(Path(record['source']), Path(record['build']),
                           Path(config['repo_root']) / POLICY['phase_patch'])
    if digest(current) != digest(record['fingerprint']):
        raise ValueError('RUNTIME_FALLBACK_CURRENT_IDENTITY_MISMATCH')
    return {**current, 'runtime_fallback_manifest': preflight.identity(manifest_path),
            'runtime_fallback_approval_sha256': digest(approval)}
