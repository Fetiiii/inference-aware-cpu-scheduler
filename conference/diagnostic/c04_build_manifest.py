#!/usr/bin/env python3
"""Create and verify provenance for the separate C04 worker-tail build."""

import argparse
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

PINNED = "571d0d540df04f25298d0e159e520d9fc62ed121"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cache_values(path):
    wanted = {"CMAKE_BUILD_TYPE", "BUILD_SHARED_LIBS", "GGML_NATIVE", "GGML_CPU_REPACK", "GGML_LLAMAFILE", "GGML_OPENMP", "GGML_OPENMP_ENABLED", "LLAMA_BUILD_SERVER", "LLAMA_BUILD_UI", "LLAMA_USE_PREBUILT_UI", "LLAMA_BUILD_COMMIT", "LLAMA_BUILD_NUMBER", "CMAKE_CXX_COMPILER"}
    values = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line or "=" not in line:
            continue
        key = line.split(":", 1)[0]
        if key in wanted:
            values[key] = line.split("=", 1)[1]
    return values


def contains_any(paths, needle):
    return any(needle in path.read_bytes() for path in paths if path.is_file())


def verify_embedded_commit(version_output):
    match = re.search(r"version:\s*\d+\s*\(([0-9a-fA-F]{7,40})\)", version_output)
    if not match or not PINNED.startswith(match.group(1).lower()):
        raise ValueError("embedded llama.cpp --version revision does not match pinned source")
    return match.group(1).lower()


def ggml_commit_metadata(commands):
    for entry in commands:
        if not entry.get("file", "").endswith("/ggml.c"):
            continue
        arguments = entry.get("arguments") or shlex.split(entry.get("command", ""))
        for argument in arguments:
            if argument.startswith("-DGGML_COMMIT="):
                return argument.split("=", 1)[1].strip('"')
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write C04 diagnostic build manifest")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--phase-patch", required=True)
    parser.add_argument("--c04-patch", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if Path(args.output).exists():
        parser.error("refusing to overwrite an existing build manifest")
    source, build = Path(args.source_dir).resolve(), Path(args.build_dir).resolve()
    server = (build / "bin" / "llama-server").resolve()
    cache = build / "CMakeCache.txt"
    compile_commands = build / "compile_commands.json"
    for required in (server, cache, compile_commands):
        if not required.exists():
            parser.error(f"missing build artifact: {required}")
    values = cache_values(cache)
    required_values = {"CMAKE_BUILD_TYPE": "Release", "BUILD_SHARED_LIBS": "ON", "GGML_NATIVE": "ON", "GGML_CPU_REPACK": "ON", "GGML_LLAMAFILE": "ON", "GGML_OPENMP": "ON", "LLAMA_BUILD_SERVER": "ON", "LLAMA_BUILD_UI": "OFF", "LLAMA_USE_PREBUILT_UI": "OFF"}
    for key, expected in required_values.items():
        if values.get(key) != expected:
            parser.error(f"{key} must be {expected}; got {values.get(key)}")
    compiler = values.get("CMAKE_CXX_COMPILER")
    compiler_identity = subprocess.run([compiler, "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    version = subprocess.run([str(server), "--version"], check=True, capture_output=True, text=True)
    version_output = (version.stdout + version.stderr).strip()
    try:
        embedded_commit = verify_embedded_commit(version_output)
    except ValueError as exc:
        parser.error(str(exc))
    binaries = list((build / "bin").glob("*"))
    shared_libraries = sorted({path.resolve() for path in binaries if ".so" in path.name and path.is_file()})
    if not shared_libraries:
        parser.error("shared diagnostic libraries missing")
    capabilities = []
    if contains_any(binaries, b"PHASE_MARK"):
        capabilities.append("PHASE_MARK")
    if contains_any(binaries, b"C04_WORKER_TAIL_TRACE_PATH"):
        capabilities.append("C04_WORKER_TAIL")
    if set(capabilities) != {"PHASE_MARK", "C04_WORKER_TAIL"}:
        parser.error("built runtime does not expose both diagnostic capabilities")
    commands = json.loads(compile_commands.read_text(encoding="utf-8"))
    compile_entries = [entry for entry in commands if entry.get("file", "").endswith(("ggml-cpu.c", "llama-context.cpp"))]
    manifest = {
        "schema_version": 1, "source_commit": PINNED,
        "source_dir": str(source), "build_dir": str(build),
        "patch_sha256": {"phase_mark": digest(args.phase_patch), "c04_worker_tail": digest(args.c04_patch)},
        "cmake_cache": values, "compiler_identity": compiler_identity,
        "historical_compiler_identity": "GNU 16.1.1",
        "compiler_warning": (None if "16.1.1" in compiler_identity else
                             "Compiler differs from historical Intel GNU 16.1.1; both diagnostic arms use this same recorded build."),
        "compile_commands": compile_entries,
        "binary": {"path": str(server), "sha256": digest(server), "version_output": version_output,
                   "embedded_source_commit": embedded_commit},
        "c04_build_amendment": "UI building and prebuilt UI provisioning disabled; not historical C01/C02 settings",
        "ggml_build_metadata": {
            "commit_define": ggml_commit_metadata(commands),
            "is_source_provenance": False,
            "note": "GGML may report the enclosing parent repository revision for an archived source tree; pinned source and embedded llama.cpp identity are recorded separately.",
        },
        "shared_libraries": [{"path": str(path), "sha256": digest(path)} for path in shared_libraries],
        "source_file_sha256": {name: digest(source / name) for name in
                               ("ggml/include/ggml-cpu.h", "ggml/src/ggml-cpu/ggml-cpu.c", "src/llama-context.cpp")},
        "capabilities": capabilities,
        "trace_semantics": "first 16 internally marked unbatched decode graph computes; top-level inter-node barrier; MUL_MAT only",
    }
    output = Path(args.output)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
