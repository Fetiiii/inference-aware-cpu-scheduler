# C04 worker-tail diagnostic build

This diagnostic is a separate build layered on pinned llama.cpp commit
`571d0d540df04f25298d0e159e520d9fc62ed121`. Apply
`llama_cpp_phase_mark.patch`, then `c04_worker_tail.patch`. It preserves the
historical Intel configuration (`GGML_NATIVE=ON`, Release, shared libraries,
OpenMP, CPU_REPACK, LLAMAFILE, CPU-only server) and does not overwrite the
frozen C01/C02 build. C04A is frozen at
`bb9e078c9bb1e8659aae6e6d3d947a2804c0e7e9`.

The phase marker keeps its existing meaning: the first internally marked
unbatched decode computation. The C04 patch uses that diagnostic boundary
only to arm collection; it does not alter affinity or scheduling.

For the first 16 unbatched decode graph computes, every worker records
timestamps immediately before and after the top-level inter-node barrier for
`GGML_OP_MUL_MAT` nodes that have a following inter-node barrier. The terminal
graph-output node is excluded: its graph-completion barrier is a different
measurement point. A fixed 131,072-event BSS buffer is reserved at
build time. The hot path allocates no memory, takes no explicit lock, and does
no file I/O. The trace is dumped after the window. Any dropped event makes a
run invalid. Per-event records include output and both operand shapes,
operation/src0 types, graph epoch, node/worker IDs, CPU samples, and
`CLOCK_MONOTONIC` timestamps. The buffer slot is reserved and metadata written
before node start; arrival/exit timestamps directly bracket the existing
barrier, without file I/O or a shared-buffer reservation between them.

Build with:

```bash
C04_WORKER_TAIL_SOURCE_DIR="$PWD/.c04/llama.cpp-worker-tail-noui-src" \
C04_WORKER_TAIL_BUILD_DIR="$PWD/llama.cpp/build-c04-worker-tail-noui" \
./conference/diagnostic/build_c04_worker_tail.sh
```

## C04 build-only no-UI amendment

The recovery build explicitly sets `LLAMA_BUILD_UI=OFF` and
`LLAMA_USE_PREBUILT_UI=OFF` to disable npm-based UI building and prebuilt UI
downloads. These are new C04 build-only settings, not historical C01/C02
settings. Native compilation, CPU_REPACK, OpenMP, ISA settings, source/patches,
workload, worker counts and experimental arms are unchanged. The upstream
asset target may still perform local embedding work; it must not install npm
dependencies or download assets.

The aborted `.c04/llama.cpp-worker-tail-src/`,
`llama.cpp/build-c04-worker-tail/` and
`results/conference_c04/build_preflight_2UsmbF2Q/` are retained as evidence.
The clean `*-noui-src` and `*-noui` directories above do not reuse that build's
UI directory or node_modules. Existing source/build paths still cause an
abort instead of an overwrite.

The manifest requires both UI cache options to be OFF and validates the
embedded llama.cpp revision reported by `llama-server --version` against the
pinned source. This version check does not load a model. The auxiliary GGML
commit definition is recorded separately: archived source under the project
may report the parent repository revision in that field. It is not source
provenance and must not replace the pinned archive or embedded llama identity.

The helper refuses to overwrite its source or build directories. The emitted
`c04_build_manifest.json` records source/patch hashes, compiler identity,
CMake state, selected compile commands, and binary/shared-library hashes. The compiler
actually available on the Intel machine is recorded; the historical compiler
was GNU 16.1.1 and any difference must be considered during checkpoint review.
The helper refuses ambient CFLAGS/CXXFLAGS/CPPFLAGS/LDFLAGS overrides and
pins the embedded runtime source revision explicitly, so archive placement
inside the parent repository does not mislabel the source commit.

The runner arms collection after its warmup request. With tracing disabled,
no arm file is consumed and no trace is emitted. With tracing enabled, the
arm file is consumed once and only the following 16 unbatched graph computes
are collected. OpenMP default placement must be retained; the preflight
rejects OMP/GOMP/KMP overrides rather than silently inheriting another
affinity policy.

Static inspection and this diagnostic do not establish an AVX/vector-width,
cache, bandwidth, or synchronization cause. Vector-width ablation remains
closed.
