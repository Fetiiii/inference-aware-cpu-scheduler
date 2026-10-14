"""Model-free native unit test of the exact gate added by the C04 patch.

Only trace-control functions are mocked. No GGML kernel or inference is run.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def native_gate(tmp_path_factory):
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler unavailable for model-free gate unit test")
    patch = (ROOT / "conference/diagnostic/c04_worker_tail.patch").read_text()
    context_patch = patch.split("+++ b/src/llama-context.cpp", 1)[1]
    patched = "\n".join(line[1:] for line in context_patch.splitlines() if line.startswith(("+", " ")))
    start = patched.index("    // C04 diagnostic-only trace gate.")
    before_compute = patched[start:patched.index("    int n_threads", start)]
    end_call = patched.index("        ggml_cpu_worker_tail_trace_end();")
    after_compute = patched[patched.rfind("    if (c04_trace_this_graph)", 0, end_call):]
    after_compute = after_compute[:after_compute.index("    // fprintf(stderr, \"splits:")]
    gate = before_compute + after_compute
    source = r'''
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <unistd.h>
static unsigned begins = 0, dumps = 0, resets = 0;
void ggml_cpu_worker_tail_trace_reset() { ++resets; }
void ggml_cpu_worker_tail_trace_begin(uint64_t epoch) {
    if (epoch != ++begins || epoch > 16) std::abort();
}
void ggml_cpu_worker_tail_trace_end() {}
int ggml_cpu_worker_tail_trace_dump(const char * path, uint32_t count) {
    if (count != 16 || begins != 16) std::abort();
    ++dumps;
    FILE * file = std::fopen(path, "w");
    if (!file) return -1;
    std::fputs("mock trace dump\n", file);
    return std::fclose(file);
}
void graph_compute(bool batched) {
''' + gate + r'''
}
int main(int argc, char ** argv) {
    if (argc != 4) return 10;
    const bool enabled = argv[1][0] == '1';
    if (enabled) {
        setenv("C04_WORKER_TAIL_TRACE_PATH", argv[2], 1);
        setenv("C04_WORKER_TAIL_ARM_FILE", argv[3], 1);
    } else {
        unsetenv("C04_WORKER_TAIL_TRACE_PATH");
        unsetenv("C04_WORKER_TAIL_ARM_FILE");
    }
    graph_compute(false); // warmup, before the arm file exists
    if (begins || dumps || resets) return 11;
    FILE * arm = std::fopen(argv[3], "w");
    if (!arm) return 12;
    std::fclose(arm);
    for (unsigned i = 0; i < 3; ++i) graph_compute(true);
    if (begins || dumps) return 13; // no prefill collection
    for (unsigned i = 0; i < 15; ++i) graph_compute(false);
    if (dumps) return 14;
    graph_compute(false);
    for (unsigned i = 0; i < 20; ++i) graph_compute(false);
    if (enabled && (begins != 16 || dumps != 1 || resets != 1)) return 15;
    if (!enabled && (begins || dumps || resets)) return 16;
    return 0;
}
'''
    directory = tmp_path_factory.mktemp("c04_gate_native")
    source_path, binary = directory / "gate.cpp", directory / "gate"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run([compiler, "-std=c++17", "-O0", str(source_path), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("enabled", [False, True])
def test_actual_patch_gate_disabled_or_exactly_16_decode_graphs(native_gate, tmp_path, enabled):
    trace, arm = tmp_path / "trace.jsonl", tmp_path / "request.arm"
    subprocess.run([str(native_gate), "1" if enabled else "0", str(trace), str(arm)],
                   check=True, capture_output=True, text=True)
    assert trace.exists() is enabled
    assert arm.exists() is (not enabled)
