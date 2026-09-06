# TASK-C03 observation summary

Selected C03 path: **CROSS_VENDOR**. This is a descriptive minimal generality check; it does not automatically declare that placement behavior or external observability generalizes.

The completed full pilot contains 6 randomized rounds and 12 valid measured
runs, with n=6 per arm.

The labels `big` and `compact` are operational labels supplied by the experiment configuration. They do not assert architectural equivalence to Intel P/E cores.

## Performance by arm

| arm | n | TTFT mean / median ms | ITL p95 mean / median ms | decode tps mean |
|---|---:|---:|---:|---:|
| BIG_ONLY | 6 | 15437.755 / 15346.599 | 109.055 / 108.29 | 9.363 |
| ALL_CORES | 6 | 9035.452 / 8946.066 | 100.666 / 100.499 | 10.422 |

## ALL_CORES minus BIG_ONLY

Positive latency values mean ALL_CORES was slower; positive throughput means ALL_CORES had higher throughput. These are observations, not a generality verdict.

| metric | absolute difference | relative difference |
|---|---:|---:|
| ttft_ms | -6402.303 | -41.472% |
| itl_p95_ms | -8.389 | -7.692% |
| decode_tps | 1.059 | 11.31% |

## Protocol and conservative interpretation

These measurements were collected on the AMD Ryzen AI 9 HX 370 using the C03
AVX2-constrained diagnostic build amendment. The amendment reduces AMD-only
AVX-512 vector-width capability as an additional cross-vendor confound. It
addresses only one vector-width difference and does not make the architectures
equivalent or isolate core topology.

On AMD Ryzen AI 9 HX 370 under the C03 AVX2-constrained configuration,
ALL_CORES improved both prefill and decode performance relative to BIG_ONLY.
Therefore, the Intel placement inversion did not reproduce on this
platform/configuration. The frozen Intel detector thresholds produced no
transitions in 12/12 runs, so zero-shot threshold transfer failed. These
results constrain cross-vendor generality but do not invalidate the Intel
external phase-recovery result. This is a valid negative generality result,
not an experiment failure. C03 did not test the microarchitectural mechanism
behind the difference, and this result does not establish universal AMD
behavior.

## External signal observations

PHASE_MARK is used only here to label samples offline. The detector ran without a marker callback and never changed affinity.

| arm | phase | n | mean | median | p05 | p95 |
|---|---|---:|---:|---:|---:|---:|
| BIG_ONLY | PREFILL | 4434 | 6.308 | 0.0 | 0.0 | 37.5 |
| BIG_ONLY | DECODE | 7802 | 12.426 | 0.0 | 0.0 | 50.0 |
| ALL_CORES | PREFILL | 2569 | 28.977 | 16.667 | 0.0 | 102.609 |
| ALL_CORES | DECODE | 6257 | 4.817 | 0.0 | 0.0 | 17.241 |

## Frozen-threshold behavior

Frozen zero-shot detector: interval=20 ms, hi=3000, lo=2100, k=2.
- BIG_ONLY: transitions 0 / 6; detect relative to internal boundary None
- ALL_CORES: transitions 0 / 6; detect relative to internal boundary None

The frozen Intel thresholds produced 0 / 12 transitions in total
(BIG_ONLY: 0 / 6; ALL_CORES: 0 / 6), so zero-shot threshold transfer failed in
the full pilot. The reported signal distributions may still contain phase
information, but this pilot does not establish generalized cross-platform
external observability.

Offsets are external criterion crossings relative to the first internally marked unbatched decode computation. Negative values are not automatically labeled prediction or anticipation.

## Checkpoint questions

- Does ALL_CORES change TTFT in the expected direction?
- Does it also change decode-tail latency or throughput?
- Are prefill/decode signal distributions observably distinct?
- Did the unchanged frozen threshold transition in every run?
- Do temperature ranges suggest a machine-state confound?

## Raw artifact archival note

The PR contains derived outputs and preflight metadata. The complete
collaborator-side `results/conference_c03/` directory and its raw detector,
phase, and server logs must be archived separately before the final paper
evidence freeze. Those raw logs are not tracked in this PR; no absent raw
artifact is reconstructed or represented by this note.
