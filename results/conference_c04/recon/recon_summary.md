# TASK-C04A reconnaissance summary

This checkpoint is static/read-only reconnaissance. It contains no new LLM
benchmark and no causal mechanism claim.

## 1. Is the actual Q4_K decode hot path identified with high confidence?

**YES.** For the model's two-dimensional Q4_K weights, the pinned CPU build
selects CPU_REPACK's `block_q4_K` 8x8 / Q8_K trait. Unbatched one-row decode
selects `ggml_gemv_q4_K_8x8_q8_K`. The conclusion is supported jointly by the
GGUF tensor types/shapes, source dispatch, emitted build flags, retained
symbols, and compiled code. This does not claim the symbol is all of decode;
Q4_K_M contains mixed tensor types and no time-share profile was run.

## 2. What SIMD/vector behavior is directly observed?

The compiled symbol contains XMM and predominantly YMM instructions, with no
ZMM references. Directly observed operations include AVX/AVX2 loads,
broadcasts, shifts, masks, byte/word unpack and shuffle operations,
`vpmaddubsw`/`vpmaddwd` integer dot-product accumulation, conversion to float,
and 256-bit floating-point FMA. This is an instruction-path observation, not
an explanation of the placement inversion.

## 3. Are usable unprivileged perf counters available?

**NO, in the present environment.** The kernel has `CONFIG_PERF_EVENTS=y` and
`kernel.perf_event_paranoid=2`, but the Linux `perf` executable is absent. A
trivial-process `perf stat` and supported-event query therefore could not run.
No package was installed and no system setting was changed.

## 4. Does existing evidence already show clean P/E worker straggler behavior?

**INCONCLUSIVE.** C01 shows phase-specific P/E residency and C02 shows the
aggregate P8-vs-P8+E8 decode regression. The runtime has per-node barriers, but
no existing artifact records per-worker node completion or barrier arrival.
The structural possibility is not a measurement of straggling.

## 5. Is one follow-up runtime diagnostic justified?

**YES.** A single two-arm Intel diagnostic that records bounded, in-memory
per-worker arrival times at the existing post-node barrier would directly test
whether P8+E8 produces a class-correlated completion tail. The design is in
`existing_evidence.md`; it has not been implemented or run.

## 6. Is vector-width ablation justified yet?

**NO.** The actual kernel does use 256-bit AVX2/FMA, but static instruction
presence neither demonstrates P/E worker straggling nor identifies vector
width as causal. The bounded runtime diagnostic should be reviewed before any
128-vs-256 vector-width experiment or synthetic kernel is opened.

## Checkpoint boundary

The evidence supports one minimal runtime-timing diagnostic, not a mechanism
finding. Barrier waiting, L2 sharing, bandwidth saturation, and AVX execution
asymmetry remain untested hypotheses. There are no proposed commands to run
until ChatGPT explicitly authorizes a future diagnostic.
