# Failure, cancellation and resume policy

The workflow never replaces a technically valid observation because it is slow, disappointing, thermally warned, or makes a hypothesis fail. All attempts remain in the evidence and failure census.

Only `startup_timeout` and `connection_before_tokens` may receive a technical retry, and only with zero generated tokens. Permit at most one extra attempt per logical slot and four extra attempts across the entire session. Preserve the original attempt, reason, token count and retry counters. A malformed trace, marker/actuation mismatch, mid-stream failure, timeout after tokens or valid warned observation does not become a retry opportunity. Exhaustion or an unclassified failure stops measurement for review.

Safety failures (AC loss, critical temperature, missing sensor, machine/identity drift or time budget) stop new requests. Preserve partial outputs. Cleanup is limited to the server/observers owned by this session. Do not kill unrelated processes or alter power/scheduler state. Cancellation records its time, current slot, token count and owned-process cleanup result; then use the reserved archive time to package all evidence.

Use Ctrl-C for supervised cancellation; SIGTERM is routed through the same
cleanup/final-archive path. Cancellation between requests may resume the same
session; an interrupted active request cannot be replaced automatically.
SIGKILL, power loss and disk/filesystem failure cannot guarantee finalization.
On recovery, use `package --session ...` without resuming inference. An unfinished
attempt/session fails closed and requires author review. Archive failures retain
the partial archive and raw directory and never print a successful session.

Thermal warnings on otherwise technically valid final observations stay in the primary analysis. Report complete-pair thermal sensitivity separately. Any training/validation warning blocks conditional policy eligibility. Do not recollect training/validation to erase the warning.

Resume is a continuation of the same frozen session, not a new sampling opportunity. It requires the same approval/plan/package/configuration/identities and persisted schedule, ledger, cohort/model hashes, completed slots and retry counters; recheck preflight/machine state and remaining three-hour budget. Never overwrite completed artifacts or reset counters/time budget. Preserve pending or interrupted slot status. If integrity is uncertain, a request generated tokens, a confirmatory pair is incomplete, or the implementation cannot demonstrate safe continuation, stop and archive for author review rather than automatically resuming measurement. Packaging must remain possible without resuming inference.

A NOT_ELIGIBLE policy gate skips the complete policy cohort and records reasons; it is an interpretable outcome, not a retryable error. Missing final round pairs block the corresponding contrast; no replacement, artificial pairing or silent sample-size reduction is allowed. A reduced design requires a new reviewed protocol, new approval and explicit disclosure, not an in-session edit.

The final amendment approves BOTH schedules and a timing-only rule before smoke,
then freezes one tier after exactly four excluded technical smoke requests and
before exploration. FULL has72 maximum/48 when the
scientific gate fails; STATIC_SIGNAL has48 and policy NOT_PLANNED regardless of
results. Neither fitting means STOP/archive. There is no later tier downgrade. A single six-request training
configuration is selected from exploration only. Failed recalibration never
authorizes another configuration, detector bank or held-out retuning. Resume
pauses count toward the same wall-clock budget; clock rollback stops for review.

The budget starts with preflight preparation and includes any separately approved
runtime fallback. Only its missing/incompatible-runtime gate may be deferred;
all other gates remain hard failures. A fallback build has a900s cap, isolated
paths, owned-process-group cancellation and no automatic retry. Its failed logs
are archived even when no request ran. Initial config/preflight remain unchanged;
hash-bound execution overlays identify the prospective new runtime. No package
installation/download/system-setting repair is permitted.

After policy eligibility, an EXTERNAL criterion that never crosses on a fresh
request is a valid `NO_EXTERNAL_TRIGGER` outcome. Keep its observed performance
and mask, with no retry/replacement; do not condition the final comparison on
successful switches. A trigger that cannot reach the actuator, failed TID
affinity calls, or an ORACLE missing its required marker action are technical
failures and stop the session. Planned masks are distinguished from observed
per-thread eligibility/residency.
