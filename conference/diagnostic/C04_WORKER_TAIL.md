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
./conference/diagnostic/build_c04_worker_tail.sh
```

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
