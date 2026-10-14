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
- Two randomized rounds, each arm once, order-seed family rooted at `4404`.
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
The order family uses `Random(4404 + round)` separately in each round.
Both rounds happen to order `PE_ALLOWED_T8_TB16` then `P8_ONLY` for this seed;
the randomized design is not counterbalanced.
The model and prompt hashes are checked against the frozen Intel records;
`--model` and `--prompt` may relocate those same inputs, not substitute another
model or workload.

## Build and preflight

```bash
./conference/diagnostic/build_c04_worker_tail.sh
python3 conference/experiments/c04_worker_tail.py --preflight-only
```

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
