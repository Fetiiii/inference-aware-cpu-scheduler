# TASK-C03 AMD cross-vendor smoke runbook

## Frozen scope

**SELECTED C03 PATH: `CROSS_VENDOR`**

The target is an AMD Ryzen AI 9 HX 370 with 12 physical cores and 24 logical
CPUs. C03 changes one generality axis: CPU vendor/platform. The authorized
sequence began with a two-round, four-request smoke and, after review, extended
to a completed six-round full pilot with 12 valid measured requests (n=6 per
arm). It contains only `BIG_ONLY` and `ALL_CORES`; it does not add stock,
external/oracle scheduling, intermediate splits, contention, sched_ext, or a
second model-family axis.

No additional run is authorized by this runbook. Review the completed pilot
before any new experiment and do not start C04.

## Collaborator quick start

Clone the `AMD` branch, including the pinned llama.cpp submodule:

```bash
git clone --branch AMD --recurse-submodules <REPOSITORY_URL>
cd inference-aware-cpu-scheduler
```

The repository does not contain or guess a model download URL. Place the
exact `Qwen3.5-9B-Q4_K_M.gguf` at
`models/Qwen3.5-9B-Q4_K_M.gguf`, or pass its existing path to preflight.

Preflight never builds or starts inference. On a fresh clone it will therefore
tell you to build the diagnostic runtime first:

```bash
./conference/tools/c03_cross_vendor.sh build-diag
```

This explicit subcommand checks for `git`, `cmake`, `c++`, `make`, and `python3`,
initializes the pinned submodule if needed, applies the frozen diagnostic
patch, and builds `llama-server` locally. It never installs packages or invokes
`sudo`.

Then run the required gate, adding `--model /absolute/path/Qwen3.5-9B-Q4_K_M.gguf`
if the model is elsewhere:

```bash
./conference/tools/c03_cross_vendor.sh preflight
```

Only after it prints `PRECHECK STATUS: PASS`:

```bash
./conference/tools/c03_cross_vendor.sh smoke
```

The smoke automatically runs the analyzer and prints the collaborator handoff.

After the smoke output has been reviewed and explicit continuation is approved,
the 6-round pilot extends the plan while selecting only absolute rounds 3–6, preserving
completed smoke rounds 1–2:

```bash
./conference/tools/c03_cross_vendor.sh pilot
```

Analysis can also be regenerated without inference:

```bash
./conference/tools/c03_cross_vendor.sh analyze
```

Smoke and pilot consume the model path and hashes persisted by preflight; they do not
accept a replacement.

## Diagnostic build provenance

The pinned llama.cpp submodule commit is:

```text
571d0d540df04f25298d0e159e520d9fc62ed121
```

The C01/C02 diagnostic source modification had been temporary and
uncommitted, although the compiled binary was preserved. Historical object
code establishes that it used `clock_gettime(CLOCK_MONOTONIC)`, emitted the
marker on the first graph computation and batched-state changes, and flushed
this exact record:

```text
PHASE_MARK batched=%d t_mono_ns=%lld
```

That instrumentation is now frozen in
`conference/diagnostic/llama_cpp_phase_mark.patch`; its semantics are described
in `conference/diagnostic/PHASE_MARK.md`. The AMD machine builds from the same
pinned source plus that patch. Do not copy the Intel-built executable.

### Historical Intel C01/C02 build record

The frozen historical Intel diagnostic build policy was:

```text
CMAKE_BUILD_TYPE=Release
BUILD_SHARED_LIBS=ON
GGML_NATIVE=ON
GGML_OPENMP=ON
GGML_OPENMP_ENABLED=ON
LLAMA_BUILD_SERVER=ON
```

This historical C01/C02 record is preserved as written; it was not an
AVX2-constrained build.

### C03 AMD protocol amendment

The official AMD HX 370 C03 pilot intentionally used the build helper's
AVX2-constrained diagnostic configuration:

```text
CMAKE_BUILD_TYPE=Release
BUILD_SHARED_LIBS=ON
GGML_NATIVE=OFF
GGML_AVX=ON
GGML_AVX2=ON
GGML_FMA=ON
GGML_F16C=ON
GGML_AVX512=OFF
GGML_AVX512_VBMI=OFF
GGML_AVX512_VNNI=OFF
GGML_AVX512_BF16=OFF
GGML_OPENMP=ON
GGML_OPENMP_ENABLED=ON
LLAMA_BUILD_SERVER=ON
```

This protocol amendment uses an AVX2-constrained build to reduce AMD-only
AVX-512 vector-width capability as an additional cross-vendor confound. It is
only a reduction of one vector-width difference: it does not make the two
architectures equivalent or isolate core topology. The AMD AVX2 full pilot
remains the official C03 dataset. Missing dependencies are reported but never
installed automatically.

Preflight verifies the exact marker format in the executable or its sibling
shared libraries. The semantic ground truth remains the first measured-request
marker with `batched=0`: the first internally marked unbatched decode
computation. It labels samples offline only and never affects the external
detector or placement.

## Conservative topology selection

No CPU ID is hard-coded. Preflight:

1. reads online and allowed CPUs;
2. groups logical CPUs into physical cores using package/core IDs and each
   CPU's `thread_siblings_list`;
3. keeps SMT enabled but chooses one allowed logical representative per
   physical core;
4. seeks a clean 4-core higher-performance and 8-core compact/lower-performance
   split, preferring CPPC `highest_perf`, then static maximum frequency, then
   another exposed capacity class;
5. rejects disagreeing hardware classifications, overlaps, duplicate SMT
   siblings, offline CPUs, and CPUs outside the caller's allowed affinity.

The frozen experimental masks contain four physical-core representatives for
`BIG_ONLY` and those four plus eight compact representatives for `ALL_CORES`.
Thread counts are 4 and 12 respectively.

Transient current frequency is not used for classification. If no clean split
exists, preflight returns `PRECHECK_NEEDS_REVIEW`, writes a physical-core table,
and does not create a smoke-authorizing topology file. Do not guess masks.

`--allow-non-hx370` exists only to collect debugging evidence on another AMD
topology. It can never produce a smoke-authorizing PASS.

## Preflight evidence and machine hygiene

Preflight preserves the following under
`results/conference_c03/preflight/`:

```text
topology.json
environment.txt
selected_topology.json       # PASS only
c03_topology.env             # PASS only; consumed by smoke
preflight_status.json
preflight_summary.md
history/                     # prior preflight snapshots
```

The evidence includes kernel/uname, CPU model and vendor, full `lscpu` and
extended topology, online/allowed masks, physical IDs and SMT siblings,
per-CPU maximum frequency, CPPC performance fields, scaling driver, governor,
EPP, amd_pstate status/prefcore, boost, power profile, AC state, readable
temperatures, Git commit/dirty state, submodule commit, diagnostic patch/build
identity, compiler, binary hash, model path/hash, and marker capability.

Run plugged in under one fixed power profile. Preflight warns but never changes
anything when it sees battery operation, mixed governors/EPPs, disabled boost,
or an already-high temperature. Do not try to match the Intel machine's
wattage; C03 checks effect direction, not normalized cross-machine scores.

## Smoke fail-safe and frozen protocol

Before inference, `smoke` revalidates the persisted PASS against live online
CPUs, allowed affinity, physical-core membership, Git commit, model hash,
diagnostic build/patch/CMake identity, exact marker format, and any existing
C03 plan. It consumes `c03_topology.env`; it never redetects or silently
changes masks.

The launcher passes exactly:

```text
--path CROSS_VENDOR
--rounds 2
--order-seed 3304
--detector-mode zero_shot
--interval-ms 20
--hi 3000
--lo 2100
--k 2
--ctx 2048
--batch 2048
--ubatch 512
--n-predict 256
--seed 42
--initial-cooldown 30
--cooldown 30
```

The unchanged Intel thresholds test zero-shot transfer separately from raw
signal separability. Recalibration is not part of this smoke.

The launcher calls the existing
`conference/experiments/c03_generality.py`; it contains no duplicate benchmark
implementation. A failed gate, incompatible plan, missing marker/model/build,
or ambiguous topology stops before inference. No privileged command,
scheduler change, or system-setting mutation is performed.

## Outputs and handoff

Raw evidence remains under:

```text
results/conference_c03/raw/
  environment/
  plan.json
  runs/
  detector/
  phase_logs/
  server_logs/
```

Derived artifacts are:

```text
results/conference_c03/c03_runs.csv
results/conference_c03/signal_summary.csv
results/conference_c03/summary.json
results/conference_c03/summary.md
```

After four successful runs the launcher prints precheck status, CPU model,
masks, thread counts, Git commit, binary/model identities, run status, and
output directory. Send back the complete directory:

```text
results/conference_c03/
```

The PR contains derived CSV/Markdown output and preflight metadata, but it does
not track the raw detector, phase, or server logs. The complete
collaborator-side `results/conference_c03/` directory, including those raw logs,
must be archived separately before the final paper evidence freeze. Do not
reconstruct or invent absent raw artifacts.

The analyzer reports observations only. It does not automatically claim that
placement behavior, signal separation, or the frozen threshold generalizes.
