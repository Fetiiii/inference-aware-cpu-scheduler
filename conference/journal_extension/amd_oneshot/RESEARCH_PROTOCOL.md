# JE-AMD-01-v3 — final pre-execution readiness amendment

Git-native delivery amendment: scientific plan/cohorts/rules below are unchanged.
The private historical C03 archive is verified on the author side, not bundled
or required during new collaborator measurements. Minimal tracked identity
attestation supplies the unchanged runtime/model anchors without claiming local
raw-data presence. New exact-commit Git approval is required; old ZIP approval is
not reused. See GIT_DELIVERY_TR.md and PROVENANCE_POLICY.md.

Status: **NOT APPROVED FOR COLLABORATOR EXECUTION**. This is a new journal-extension study, not a rewrite of C03. Prior v1/v2 source/plans remain byte-preserved in their prep ZIPs. No new request has been run. Author approval must bind plan/source fingerprint, both complete schedules, exact outcome-blind duration rule, optional fallback policy and session budget before deployment. It is not a prospective preregistration of old C03 observations.

## Historical anchor and scope

The explicitly supplied private C03 ZIP has SHA256 `02bbc0f878667fb71f3cbf07b3367c648e152889ff27c3047050fd831744ab07`. Safe ingestion established 12 complete run/detector/phase/server reference sets, two original environments, the plan and preflight records. Both original environments establish model SHA256 `03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8`, prompt SHA256 `27f3f8ba7b73d3df1337549237a8e9f4ccf57e0551908cc93ce8a183480751b8`, source `571d0d540df04f25298d0e159e520d9fc62ed121` and historical server SHA256 `a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1`. Originals do not need recollection.

Historical BIG_ONLY used 4/4 workers; ALL_CORES used 12/12. Their normalized context-switch signal is zero-heavy and configuration-dependent. With the declared ±200 ms exclusion around the internal marker, all six BIG_ONLY request means rise but both-side request medians remain zero; all six ALL_CORES request medians fall. This does not identify behavior of the unmeasured ALL_ALLOWED4 or COMPACT4 configurations. A pooled global threshold/polarity is not assumed transferable. Original historical summary values are unchanged; the separate raw review describes its own window definition.

RQ1 uses controlled placement and concurrency contrasts, not a full factorial identification of architecture × concurrency interaction. RQ2 separates within-configuration observability, between-configuration transfer and Intel zero-shot transfer. RQ3 evaluates one configuration-specific detector on independent whole requests. RQ4 is conditional actionability, not a requirement to find a positive effect.

## Frozen workload and build

Existing Qwen3.5-9B-Q4_K_M.gguf, exact hashes above; existing pinned diagnostic runtime, CPU-only. ctx=2048, batch=2048, ubatch=512, n_predict=256, seed=42, temperature=0, ignore_eos=true, cache_prompt=false. Fresh server per request, one-token "Warmup.", 1 s settle and 0.3 s monitor preroll. Initial/between-request cooldown ≥30 s. All arms carry the same live marker watcher and 20 ms /proc acquisition path, including measured observation cost. Performance is observed with this monitoring, not claimed perturbation-free.

AMD build: Release/shared/OpenMP; GGML_NATIVE=OFF, AVX/AVX2/FMA/F16C ON, AVX512-family OFF. This AVX2-constrained build reduces vector-width differences as an additional confound; it does not establish ISA parity or isolate topology. The historical Intel C01/C02 native-ON build record remains unchanged.

This preparation authorizes no build, replacement, install, download or machine-state change. The historical runtime remains preferred and retains exact known identity checks. A separately approved optional offline build fallback uses the identical pinned source/marker and AMD AVX2 policy in isolation, records a NEW prospective runtime identity and receives a full preflight before requests. Old performance is never pooled as identical-build repetition. Previously unrecorded libraries are prospectively frozen, not invented historical hashes. See PROVENANCE_POLICY.md.

## Static arms

Automatic validated AMD HX370 topology: four big and eight compact physical cores, 24 logical CPUs, one allowed/online logical representative per physical core. Operational labels do not assert cross-vendor architectural equivalence.

| Arm | Eligibility | threads / threads-batch |
|---|---|---|
| BIG4 | four big representatives | 4 / 4 |
| COMPACT4 | deterministic first four compact physical cores | 4 / 4 |
| ALL_ALLOWED4 | all twelve physical representatives | 4 / 4 |
| ALL_CORES12 | identical twelve representatives | 12 / 12 |

COMPACT4 is never performance-selected. Actual per-thread CPU observations and affinity masks remain distinct from eligibility and worker count.

## Cohorts and frozen tier

| Cohort | Requests | Permitted use |
|---|---:|---|
| technical_smoke | 1 complete round × 4 = 4 | excluded technical checks and duration forecasting |
| exploration | 2 complete rounds × 4 = 8 | endpoint/comparator selection only |
| calibration | 6 fresh requests at ONE frozen initial configuration | detector fitting only |
| validation | 6 fresh same-configuration requests | detector validation and policy eligibility only |
| static_confirmatory | 6 complete rounds × 4 = 24 | static performance; descriptive frozen-detector transfer |
| policy_confirmatory | 6 complete rounds × 4 = 24, conditional | fresh policy performance |

FULL has maximum72 requests, or48 if scientific policy is NOT_ELIGIBLE. STATIC_SIGNAL has the identical first48 requests and policy NOT_PLANNED. Base plan AUTO binds BOTH schedules. After exactly four excluded technical smoke requests and before any exploration, forecast FULL including elapsed setup/build/smoke,25% request allowance,30s cooldown,600s archive reserve and300s safety. If it fits10800s freeze FULL; otherwise try STATIC_SIGNAL; otherwise STOP/archive. Only technical elapsed fields are used; never TTFT/ITL/TPS/outcomes. tier_decision.json is immutable and verified on resume; no later downgrade. Both alternatives and the rule require prior author approval. See EXPECTED_DURATION.md.

Seed family 7707 with stage offsets 1000–6000 and absolute within-cohort round number freezes complete randomized orders. Selected-mask cohorts use logical arm INITIAL_SELECTED; immutable operating_configuration and resolved mask metadata identify actual placement. Selection is saved before their first request. No valid request is replaced.

## Endpoint selection before detector training

Using only the eight exploration requests, select best median TTFT and best median ITL p95 endpoints among the three 4/4 arms. Require distinct endpoints, ≥5% aggregate advantage in each opposing endpoint metric, favorable direction in both exploration rounds and no exploration thermal warnings. STATIC is selected independently by the geometric mean of normalized median TTFT and ITL p95; lexical ties are deterministic.

If there is no opportunity, still characterize one fixed reference configuration ALL_ALLOWED4; do not try alternative masks until a detector works. The policy remains NOT_ELIGIBLE even if that reference signal separates.

## One configuration, one calibrated detector

Fit exactly six whole calibration requests from the frozen initial configuration; mixed configurations are rejected. No per-arm detector bank is fitted to two-request groups. Search identity/log1p, pooled quantiles within THIS configuration {0.2,0.35,0.5,0.65,0.8}, lo<hi, rising/falling polarity and k∈{2,3}. Strict >hi/<lo crossings; invalid samples or gaps >50 ms reset persistence. Confirmation time is the kth sample.

Predeclared objective: lexicographically minimize spurious events, missed eligible transitions, median absolute matched-marker offset, then canonical parameter ID. Freeze parameters, configuration, training IDs/hash and protocol hash before six held-out requests. No historical, exploration, validation, static-final or policy-final requests enter fitting. A degenerate/no-eligible fit is a reportable negative, not a search for a different configuration.

Evaluate both frozen Intel 20 ms/3000/2100/k2 reference and recalibrated detector on the same six fresh initial-mask requests. Operational marker eligibility requires ≥3 valid samples strictly on each side outside ±200 ms. Coverage is marker agreement, not semantic recall; precision/recall remain undefined. Report misses, spurious events and all criterion offsets. Negative offsets are not prediction.

Report raw signal distributions separately for every static configuration. After fitting is frozen, run descriptive transfer evaluation on the 24 static-final requests: initial-configuration matched, other four-worker placement, and ALL_CORES12 worker-count/eligibility-confounded categories. Never tune on these final requests. No automatic generalization claim.

## Conditional policy

Require an exploration opportunity, exact selected-configuration/model correspondence, disjoint IDs/hashes, six eligible validation requests, six marker matches, zero spurious validation requests, and no calibration/validation thermal warnings. Failure skips the whole policy cohort with reasons; do not invent another policy.

All STOCK/STATIC/EXTERNAL/ORACLE arms use 4/4 workers and identical binary, workload, metrics, cooldown, monitor and watcher. STOCK inherits allowed affinity; STATIC uses its frozen comparator mask. EXTERNAL and ORACLE share the same initial/final masks and actuator. Only ORACLE routes a live PHASE_MARK to it; EXTERNAL decisions receive only external samples.

The external controller latches ONE criterion crossing while running under the initial mask. Calibration is for that trigger. After actuation the final-mask raw signal is still recorded, but no claim is made that the criterion remains a valid bidirectional classifier under a changed mask. No marker feedback or post-action retuning occurs. Final-mask signal distributions and cross-configuration transfer are reported independently.

An EXTERNAL no-trigger request is a valid policy outcome retained in performance comparisons. Affinity/routing failures, missing required ORACLE action or incomplete instrumentation are technical stops, never replacement opportunities.

## Statistics, missingness and interpretation

Six complete round-level paired differences are the independent units for confirmatory contrasts. Primary metrics: TTFT and ITL p95. Static contrasts: COMPACT4−BIG4, ALL_ALLOWED4−BIG4, ALL_CORES12−ALL_ALLOWED4. Policy: EXTERNAL−STATIC, EXTERNAL−STOCK, EXTERNAL−ORACLE. The fixed twelve-slot JE_AMD_PRIMARY_12 family uses two-sided paired-t tests, marginal 95% paired-t intervals and Holm across all twelve; unavailable/unplanned policy slots enter adjustment as p=1 bookkeeping only, never observed tests. Negative latency differences favor first arm; relative effect is 100×(mean(A)/mean(B)−1).

Independent approximately normal round differences are assumptions; six rounds cannot establish them. No token/sample/node pseudoreplication, equivalence or non-inferiority. Do not change methods after seeing outcomes. Keep all technically valid thermal-warned runs; report complete-pair un-warned sensitivity. Incomplete rounds block confirmatory inference, not a reduced-n primary test. Throughput, ITL p50/p99, residency, migrations, thermal/frequency, signal transfer and monitoring/actuation cost are secondary/descriptive.

C01–C06 and C05 results remain frozen. The recovered historical review motivated this v2 protocol; this is not a preregistration of historical observations. Negative transfer, no placement opportunity or failed recalibration are valid outcomes. No universal AMD, vector-width, bandwidth or synchronization causal conclusion follows automatically.
