# TASK-C04B — Intel worker-tail diagnostic runbook

C04B asks one bounded question: when eight decode workers may execute across
the validated Intel P+E mask, is the historical decode regression accompanied
by E-associated node-completion tails at the top-level graph-node barrier?
This diagnostic does not test vector width and does not establish causality.

## Frozen protocol

- Intel i7-14650HX only; P representatives `0,2,4,6,8,10,12,14`, E CPUs
  `16,17,18,19,20,21,22,23`; no P SMT siblings.
- `P8_ONLY`: mask P8, `-t 8 -tb 8`.
- `PE_ALLOWED_T8_TB16`: mask P8+E8, `-t 8 -tb 16`.
- Two counterbalanced rounds, each arm once; first-round shuffle seed `4404`.
- Same Qwen3.5-9B-Q4_K_M model and prompt; ctx/batch/ubatch
  `2048/2048/512`, 256 predicted tokens, generation seed 42,
  `cache_prompt=false`.
- Fresh server per run; at least 30 seconds initial and inter-run cooldown.
- First 16 internally marked unbatched decode graph computes only;
  `MUL_MAT` nodes followed by a top-level inter-node barrier, with Q4_K
  distinguished by `src0` type. A terminal graph-output node has no
  inter-node barrier and is excluded (including an unfused terminal LM head).

Both arms execute the identical fixed-buffer instrumentation path. Actual
CPUs are sampled at node start, barrier arrival, and barrier exit. Worker
identity is never used as a substitute for CPU class. Instrumented latency
metrics are diagnostic sanity checks, not primary paper performance results.
The first round uses `Random(4404).shuffle()`; round 2 reverses that order.
The resulting schedule is `P8_ONLY -> PE_ALLOWED_T8_TB16` in round 1 and
`PE_ALLOWED_T8_TB16 -> P8_ONLY` in round 2. Each arm therefore occupies each
position once. The plan, run JSON/CSV and environment metadata record
`schedule_design=counterbalanced_two_rounds_v1`, seed `4404` for both rounds,
and `round_order_source=seeded_shuffle` or `reverse_round_1`. Round 2 has no
independent random draw.
The model and prompt hashes are checked against the frozen Intel records;
`--model` and `--prompt` may relocate those same inputs, not substitute another
model or workload.

## Pre-measurement protocol correction

Before the first C04B build or measured run, the reviewed implementation
`1833fec` was corrected to replace its independent per-round shuffles with
the deterministic counterbalanced schedule above. The original seed family
put both arms in the same order in both rounds. This correction changes only
ordering and descriptive analysis; arms, workload, pinned source, trace window
and buffer design are unchanged. An existing plan from the previous ordering
is incompatible and is rejected without overwriting it.

The analysis also adds participation-adjusted E last-finisher enrichment.
These definitions are frozen before measurement; no C04B measured values
were used to choose them.

## Build and preflight

```bash
C04_WORKER_TAIL_SOURCE_DIR="$PWD/.c04/llama.cpp-worker-tail-noui-src" \
C04_WORKER_TAIL_BUILD_DIR="$PWD/llama.cpp/build-c04-worker-tail-noui" \
./conference/diagnostic/build_c04_worker_tail.sh
python3 conference/experiments/c04_worker_tail.py \
  --server-bin llama.cpp/build-c04-worker-tail-noui/bin/llama-server \
  --build-manifest llama.cpp/build-c04-worker-tail-noui/c04_build_manifest.json \
  --preflight-only
```

### C04 build-only no-UI amendment

The helper now requires `LLAMA_BUILD_UI=OFF` and
`LLAMA_USE_PREBUILT_UI=OFF`. This C04-only amendment disables npm provisioning
and prebuilt UI downloads; it does not describe historical C01/C02 settings.
No compute/ISA flags, instrumentation, workload or arm settings change.

Preserve the aborted `.c04/llama.cpp-worker-tail-src/`,
`llama.cpp/build-c04-worker-tail/` and
`results/conference_c04/build_preflight_2UsmbF2Q/` unchanged. Recovery uses the
new source/build directories above and never reuses old UI/node_modules data.
Do not retry if either new target already exists. Any installation/download
attempt, build or hash failure, or preflight environmental conflict requires
preserving diagnostics and stopping for review.

Both UI cache values and the embedded llama.cpp `--version` source revision
are verified in the manifest and preflight. An auxiliary GGML commit may
identify the enclosing project rather than the archived llama.cpp source; it
is recorded separately and is not treated as source provenance.

The build helper uses the pinned llama.cpp source and applies the existing
PHASE_MARK patch before the separate C04 patch. It refuses overwrites and
emits a manifest that the runner verifies. Preflight is read-only and never
starts llama-server. It rejects a wrong CPU/topology, mismatched build or
patch/shared-library hash, unavailable inputs, nondefault OpenMP environment
overrides, and obvious concurrent CPU-heavy work. It
does not kill processes or change governor, EPP, turbo, or thermal policy.

Run plugged in, with a fixed user-selected power profile, after stopping
compilation, inference, tests, package/indexing, and other CPU-heavy agents.
Idle browser/chat applications alone are not a failure.
The process snapshot uses `ps` lifetime-average CPU usage and load averages;
it is a screening gate, not proof that all interference is absent.

The helper preserves native compilation and records the actual compiler.
The historical C01/C02 compiler was GNU 16.1.1. The compiler available during
this implementation check is GNU 16.2.1; a rebuilt diagnostic must record that
difference rather than claim bit-identical historical provenance. Both arms
must use the same new diagnostic build.

## Authorized first diagnostic (four requests only)

After implementation review, run exactly:

```bash
python3 conference/experiments/c04_worker_tail.py \
  --server-bin llama.cpp/build-c04-worker-tail-noui/bin/llama-server \
  --build-manifest llama.cpp/build-c04-worker-tail-noui/c04_build_manifest.json \
  --rounds 2 \
  --order-seed 4404 \
  --initial-cooldown 30 \
  --cooldown 30
```

There is no resume/extension mode. Existing run records cause a fail-closed
abort, and trace overflow or missing epochs stop the diagnostic while keeping
the raw run. A throttle-counter change marks the run `THERMAL_WARNING`; it is
not silently excluded.
The analyzer includes round-level observations and a separately labeled
thermal-warning-excluded sensitivity view. It makes no automatic causal or
outcome declaration.

## E last-finisher enrichment

For every complete node, the analyzer records E arrivals, total worker
arrivals, and `expected_e_last_finisher_fraction = E arrivals / all arrivals`
in `node_summary.csv`. This is a participation-only descriptive baseline,
not an assertion that workers truly have identical finishing probabilities.

Per arm, both for all selected MUL_MAT nodes and for Q4_K-backed nodes, it
reports overall E-worker arrival fraction, fraction of nodes exposed to at
least one E arrival, and fraction of E-exposed nodes with a uniquely E last
finisher. Observed and expected fractions used for enrichment are computed on
the same E-exposed nodes with a unique last finisher:

```text
observed = E-last nodes / E-exposed unique-last nodes
expected = mean(per-node E arrivals / all arrivals) over those same nodes
enrichment = observed / expected
```

The expected baseline is an unweighted mean of per-node probabilities, not a
pooled event ratio or a mean of per-node enrichment ratios. Tied-last nodes
remain in exposure counts but are excluded from both sides of enrichment;
their counts are explicit. Zero or unavailable denominators produce JSON
`null` and an NA explanation, rendered as `NA` in the Markdown enrichment
tables. Enrichment is not clipped and is not a significance or causal test.

The same comparisons are repeated after removing migrated events, using only
remaining complete worker groups and recomputing the baseline on that subset.
Remaining node counts and NA reasons are reported, including when no complete
stable-only groups remain.

Then analyze descriptively:

```bash
python3 conference/analysis/c04_analyze.py \
  --input results/conference_c04/worker_tail
```

Raw traces, per-run JSON, token timestamps, server logs, and environment state
remain under `results/conference_c04/worker_tail/raw/` and `environment/`.
Derived files are `runs.csv`, `node_summary.csv`, `worker_summary.csv`,
`summary.json`, and `summary.md`. Bring the four-run output to ChatGPT; do not
extend the run count or begin vector-width ablation automatically.

## Interpretation limits

Stable P/E labels mean the sampled start and arrival CPUs match. Migration
away and back between these samples is not observable. Matched node groups
must contain all eight workers exactly once. The migrated-event-excluded
sensitivity view uses only nodes retaining a complete worker set and reports
its remaining sample counts; it may return NA if no complete nodes remain.

The shared atomic buffer reservation occurs before the node-start timestamp.
Arrival is timestamped directly before the existing barrier, and exit directly
after it. Collection still has observer cost; barrier-wait duration includes
scheduler delay and the barrier implementation. A synchronous trace dump
after graph 16 can inflate one instrumented inter-token interval. Raw token
timestamps are retained so that effect is visible. These sanity measurements
must not replace the frozen C01/C02 performance results.

Negative arrival-offset versus barrier-wait correlation is mechanically
expected at a synchronization barrier: earlier arrivals have more time to wait
for a common release (`wait = exit - arrival`). It is not independent causal
evidence for an E-core mechanism. E participation, last-finisher enrichment,
arrival spread, migration and thermal sensitivity still require joint
checkpoint interpretation.
