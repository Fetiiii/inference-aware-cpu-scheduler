# Real-hardware compatibility hotfix

This amendment follows the `ab9d3bd` packaging review. It authorizes no request,
build, or collaborator contact. Old sessions, diagnostic ZIPs, approval files,
calibration evidence and C01–C06 records remain unchanged. A new exact-commit,
source and canonical-plan approval is required before future execution.

## Frozen patch reconstruction

The historical PHASE_MARK patch remains byte-identical (SHA256
`722adc5c8d8805d442753971d2eabd488ab91906aa8fa93961d6a615194833ce`).
Its first hunk declares original line 2438 but the exact context starts at 2439
in pinned revision `571d0d540df04f25298d0e159e520d9fc62ed121`.
The in-memory applicator now searches only ±3 original lines, requires one
unique complete match of all context/deletion lines, checks old/new counts,
and rejects partial, ambiguous, malformed or incompatible patches. No fuzzy
matching or line skipping is permitted. A real pinned-source regression compares
the reconstructed bytes with isolated `git apply` output.

A clean pinned source checkout can accompany an independently hash-anchored
historical server. It is recorded as a clean base; it is not falsely described
as that server's patched build tree. Alternatively, an exact frozen patched tree
is accepted. Other source modifications are rejected. Both require the pinned
revision, frozen patch, original server and known marker-library identities,
current compiler/cache/emitted flags and full loader closure. Missing originals
may defer only the explicit runtime gate to separately approved offline fallback;
missing provenance evidence is not waived. The fallback extracts the exact Git
revision and uses the same corrected applicator in a separate workspace.

## Memory safety review and amendment

Historical C03 tracked CSV/preflight/summary records do not establish loaded
peak RSS or minimum MemAvailable. The original private 84-member C03 ZIP was
also inspected read-only: its two environment snapshots, run JSONs and twelve
server logs provide no recorded memory peak or numeric model/KV/compute buffer
sizes. Those logs exist in the private archive, not the public tracked evidence.
Neither a historical inference peak nor a four-job compilation peak is claimed.
No new workload was run to estimate one.

The exact frozen model is 5,680,522,464 bytes (5.29 GiB). Pinned loader code
(`src/llama-model-loader.cpp`, `src/llama-mmap.cpp`) maps/prefetches model data;
KV/recurrent state, compute graphs and backend buffers are additional allocations.
The frozen CPU-only context/batch/ubatch are 2048/2048/512. Mapping size is not
peak RSS, and reclaimable file cache affects Linux MemAvailable. Do not add model
RSS to available memory or count swap as safe physical headroom.

| Stage | Minimum MemAvailable | Reason / limitation |
|---|---:|---|
| Initial preflight and before each fresh server | 8 GiB | Conservative estimate: model 5.29 GiB plus 2.71 GiB allowance for runtime/KV/compute/OS. Not an allocation bound or measured peak. |
| Loaded server / active request | 1 GiB | Emergency remaining physical headroom; separately checked after readiness and by live sampling. Does not certify peak safety. |
| Before optional offline compilation | 12 GiB | Retains the old conservative precondition because four-job build peak is unknown; no job/flag change. |
| Active compilation | 1 GiB | Periodic fail-closed emergency stop; captures readings and preserves failure diagnostics. |

Seven GiB was evaluated: after the model file alone it leaves only 1.71 GiB,
one GiB less allowance than eight. With no historical peak validating that
smaller allowance, eight is preferred. Both reject the reported 6.6 GiB available
on the 14.23 GiB-visible / 16 GB-installed HX370. The operator must reach a clean
state above the floor; the tool cannot make this desktop state safe by bypassing
the gate. Twelve GiB may also block fallback on that machine; an exact compatible
existing runtime avoids compilation but not inference safety checks.

Pre-request checks occur before creating each fresh server, so they correctly
use the pre-load floor. The loaded server is not required to retain eight GiB.
The monitor begins during startup for safety, but detector decisions stay
unarmed until the measured request; warmup/startup observations are not new
training or phase ground truth. The same path applies to every arm.
Readings record MemTotal, MemAvailable, swap totals/free, stage, floor and stop
reasons in machine/run/build evidence. Missing MemAvailable fails closed. Polling
cannot prevent sub-interval allocation spikes or guarantee absence of OOM;
failed/low-memory sessions are preserved, never automatically retried for this
reason. No swap dependence, system controls or unconditional bypass are added.

## Protocol and delivery invariants

Only safety fields of RUN_PLAN change; arms, cohorts, worker budgets, detector,
duration rule, schedule seeds, four technical smoke requests, FULL72 /
STATIC_SIGNAL48 / STOP, statistics/Holm, workload and 10,800-second total limit
remain frozen. Plan and source approval bindings are recomputed from current
bytes; old plan/source approvals fail. No existing authorization is migrated.

Use `results/journal_extension_amd_retry01` consistently, as shown in
`GIT_DELIVERY_TR.md`. Preflight never starts a server. Failed preparation still
produces a purpose-labeled private ZIP. Select the exact final archive path and
verify it with `--session` before delivery; never substitute the earlier
preflight archive or choose using a filename glob.
