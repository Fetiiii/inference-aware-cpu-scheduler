# C04A existing evidence: worker imbalance and completion tails

This review separates measurements already present from structural
plausibility and unanswered mechanism questions. It does not attribute the
Intel effect to barriers, L2 sharing, memory bandwidth, or AVX behavior.

## Observed

1. **Stock placement changes by phase but remains mixed.** C01's six-run,
   50 ms characterization reports prefill P/E residency of 52.827%/47.173%
   and decode P/E residency of 78.234%/21.766%. It also records about 8 active
   decode threads and per-thread CPU-residency samples. These are placement
   observations; sampled migrations are lower bounds.

2. **The contemporaneous static placement contrast has the inversion.** C02
   reports the following six-run means:

   | arm | mask / threads | TTFT ms | ITL p95 ms | decode tok/s |
   |---|---|---:|---:|---:|
   | STATIC_P | P8; `-t 8 -tb 8` | 10853.482 | 93.756 | 11.134 |
   | STATIC_PE | P8+E8; `-t 16 -tb 16` | 9023.523 | 108.833 | 9.657 |

   Relative to STATIC_P, STATIC_PE has 16.86% lower TTFT, 16.08% higher
   (worse) ITL p95, and 13.27% lower decode throughput. STATIC_P also recorded
   non-zero readable thermal-throttle counter deltas in all six C02 runs,
   whereas STATIC_PE recorded zero; this makes the static comparison
   thermally qualified rather than a clean mechanism attribution.

3. **Existing per-thread tooling records residency, not completion.**
   `harness/thread_residency.py` and the C01 traces record each readable TID's
   sampled CPU, scheduler state, CPU-time advancement, topology class, and
   lower-bound transitions. They do not timestamp per-worker kernel entry,
   node completion, barrier arrival, or barrier wait duration.

4. **Existing logs and counters are aggregate.** Server logs expose request
   prompt/eval timing and throughput. C01/C02 scheduler snapshots expose
   process-level migration/context-switch totals. Neither identifies the
   worker that completes a decode node last.

## Plausible from runtime structure, but not measured

- The frozen build defines `GGML_USE_OPENMP`. `ggml_graph_compute_thread()`
  executes every graph node and calls `ggml_barrier()` between nodes and after
  the graph; under this build `ggml_barrier()` maps to an OpenMP barrier.
- The Q4_K repack `forward_mul_mat()` initially gives each worker a chunk and
  then uses a shared atomic chunk counter. Dynamic chunks can mitigate some
  imbalance, while completion of the node still precedes a graph-node barrier.
- Therefore a slow last worker could structurally extend node completion.
  The current artifacts do not establish that an E-core worker is the last
  worker or that such waiting explains the observed ITL distribution.

## Unknown

- Per-worker Q4_K compute duration during unbatched decode.
- Barrier arrival spread and wait time by CPU/core class.
- Whether an E-core worker is disproportionately the last arrival.
- The fraction of decode time spent in the identified Q4_K GEMV path.
- Hardware cycles, instructions, IPC, and cache misses by arm or core class.
- Any causal role of vector width/instruction decomposition, shared L2,
  synchronization, or memory bandwidth.

## Bounded follow-up design (not implemented or run)

One runtime diagnostic is justified because the existing aggregate inversion
and barrier structure do not answer the worker-tail question.

- Machine/model: the same Intel i7-14650HX and frozen
  `Qwen3.5-9B-Q4_K_M.gguf`.
- Binary: rebuild the same pinned diagnostic source and PHASE_MARK semantics,
  changing only bounded in-memory timing instrumentation.
- Arms only:
  - `STATIC_P`: P8, `-t 8 -tb 8`.
  - `STATIC_PE`: P8+E8, `-t 16 -tb 16`.
- Observation: for a bounded set of internally marked unbatched decode graphs,
  record, in memory, each worker's monotonic arrival time immediately before
  the existing post-node barrier, `sched_getcpu()`, worker ID, node index/op,
  and Q4_K source type where applicable. Flush only after the request.
- Derived diagnostics: per-node last-arrival spread, last-arriver CPU class,
  and the fraction/distribution of Q4_K MUL_MAT nodes whose last arrival ran on
  an E core. Record instrumentation overhead using the same path in both arms.
- Interpretation: use the result only to decide whether a class-correlated
  completion tail is observed. Do not turn it into a new performance claim or
  infer a deeper cause from arrival timing alone.

No new arm, AMD run, model, synthetic microbenchmark, sched_ext policy, or
privileged counter is part of this design. Execution requires a later explicit
authorization.
