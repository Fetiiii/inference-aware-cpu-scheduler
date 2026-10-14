"""Model-free provenance checks for the C04-only no-UI build amendment."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conference" / "diagnostic"))
import c04_build_manifest as manifest  # noqa: E402


@pytest.mark.parametrize("revision", [manifest.PINNED, manifest.PINNED[:7]])
def test_embedded_llama_revision_matches_pinned_source(revision):
    assert manifest.verify_embedded_commit(f"version: 1 ({revision})\nbuilt with GNU") == revision


@pytest.mark.parametrize("revision", ["70f3bc1", "unknown", "571d0d"])
def test_parent_unknown_or_insufficient_identity_is_rejected(revision):
    with pytest.raises(ValueError, match="embedded llama.cpp"):
        manifest.verify_embedded_commit(f"version: 1 ({revision})")


def fake_build(tmp_path):
    source, build = tmp_path / "source", tmp_path / "build"
    (build / "bin").mkdir(parents=True)
    (build / "bin/llama-server").write_bytes(b"mock executable; not run")
    (build / "bin/libllama.so").write_bytes(b"PHASE_MARK C04_WORKER_TAIL_TRACE_PATH")
    for name in ("ggml/include/ggml-cpu.h", "ggml/src/ggml-cpu/ggml-cpu.c", "src/llama-context.cpp"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("mock source", encoding="utf-8")
    cache = {"CMAKE_BUILD_TYPE": "Release", "BUILD_SHARED_LIBS": "ON",
             "GGML_NATIVE": "ON", "GGML_CPU_REPACK": "ON", "GGML_LLAMAFILE": "ON",
             "GGML_OPENMP": "ON", "LLAMA_BUILD_SERVER": "ON", "CMAKE_CXX_COMPILER": "/mock/c++",
             "LLAMA_BUILD_UI": "OFF", "LLAMA_USE_PREBUILT_UI": "OFF"}
    (build / "compile_commands.json").write_text(json.dumps([
        {"file": "/source/ggml.c", "arguments": ["cc", '-DGGML_COMMIT="70f3bc1"']}
    ]))
    args = ["--source-dir", str(source), "--build-dir", str(build),
            "--phase-patch", str(ROOT / "conference/diagnostic/llama_cpp_phase_mark.patch"),
            "--c04-patch", str(ROOT / "conference/diagnostic/c04_worker_tail.patch"),
            "--output", str(build / "c04_build_manifest.json")]
    return build, cache, args


def write_cache(build, cache):
    (build / "CMakeCache.txt").write_text("\n".join(f"{key}:STRING={value}" for key, value in cache.items()))


@pytest.mark.parametrize("key", ["LLAMA_BUILD_UI", "LLAMA_USE_PREBUILT_UI"])
@pytest.mark.parametrize("value", ["ON", None])
def test_manifest_requires_both_ui_options_explicitly_off(tmp_path, monkeypatch, key, value):
    build, cache, args = fake_build(tmp_path)
    if value is None:
        del cache[key]
    else:
        cache[key] = value
    write_cache(build, cache)
    monkeypatch.setattr(manifest.subprocess, "run", lambda *a, **k: pytest.fail("invalid UI configuration reached a subprocess"))
    with pytest.raises(SystemExit) as exc:
        manifest.main(args)
    assert exc.value.code == 2
    assert not (build / "c04_build_manifest.json").exists()


def test_manifest_keeps_parent_ggml_metadata_separate_from_verified_source(tmp_path, monkeypatch):
    build, cache, args = fake_build(tmp_path)
    write_cache(build, cache)

    def mock_run(command, **kwargs):
        text = "mock compiler 16.2.1" if command[0] == "/mock/c++" else f"version: 1 ({manifest.PINNED})"
        return SimpleNamespace(stdout=text, stderr="")

    monkeypatch.setattr(manifest.subprocess, "run", mock_run)
    assert manifest.main(args) == 0
    output = json.loads((build / "c04_build_manifest.json").read_text())
    assert output["cmake_cache"]["LLAMA_BUILD_UI"] == "OFF"
    assert output["cmake_cache"]["LLAMA_USE_PREBUILT_UI"] == "OFF"
    assert output["source_commit"] == output["binary"]["embedded_source_commit"] == manifest.PINNED
    assert output["ggml_build_metadata"]["commit_define"] == "70f3bc1"
    assert output["ggml_build_metadata"]["is_source_provenance"] is False
    assert set(output["capabilities"]) == {"PHASE_MARK", "C04_WORKER_TAIL"}
