# TASK-JE-AMD-PREP-FINAL — review checkpoint

Preparation only; execution/deployment NOT APPROVED. No AMD inference, real
runtime build, download/install, privileged command, collaborator contact,
commit, push or merge occurred.

## Changes from V2

- Plan v3/AUTO retains exactly the V2 scientific arms, worker budgets, cohort
  counts, seed family, detector rules, held-out gate and twelve-slot Holm family.
- Before deployment the author approves BOTH complete schedules and the exact
  duration/fallback policy. After exactly four excluded smoke requests, a
  timing-only rule freezes FULL72, STATIC_SIGNAL48 or STOP before exploration.
  Cost=max(component sum, backend total)×1.25+30s per remaining slot. Selected
  configuration/policy slots use the maximum across all four smoke arms.
  Forecasts include elapsed preparation/build/smoke+600s archive+300s safety.
  Tier decisions carry sanitized-input/rule/schedule hashes and cannot change
  on resume; a missing previously frozen file STOPs instead of recomputing.
  No treatment-metric leakage or reduced final sample counts.
- Original historical binary/known-marker-library checks remain unchanged.
  Missing/incompatible runtime may defer ONLY that gate. An explicit separately
  approved fallback uses pinned local source+identical PHASE_MARK+AMD AVX2,
  isolated directories, existing dependencies and offline CMake options. Cap900s
  within the same10,800s session. New binary/library/compiler/flags identities
  are prospective, never fictional historical reproductions. One build attempt;
  cancellation/failure preserves diagnostics and stops before inference.
  An independent monotonic deadline prevents clock rollback extending the cap.
- Initial config/preflight are not overwritten; approved, hash-bound execution
  overlays record any new runtime and successful full preflight. Session clock
  begins with preflight preparation, including resume pauses. Authoritative and
  archived build-manifest copies plus exact diagnostic-file census are guarded.
- Exact historical ZIP remains bundled privately. No pooling of old/new build
  performance. Failed sessions, conditional skips and valid EXTERNAL no-trigger
  outcomes remain visible and archived. No positive-result-seeking fallback.

## Approval and delivery

The ZIP includes execution_approval.template.json with exact source/plan/rule/
both-schedule/fallback hashes and authorized=false. It is NOT signed approval.
The root derived document is excluded from the source-only fingerprint to avoid
a hash cycle; it is included in ZIP SHA256SUMS. Authors must review and issue a
separate immutable approval before deployment; optional fallback defaults false.

Turkish operator instructions specify existing prerequisites and exact command
forms. They require file paths/consent, not scientific choices. No dependency
installation or downloads are performed. Missing prerequisites safely stop.

## Validation and limits

See PREP_VALIDATION.md and the final checkpoint for synthetic tests, source
integrity, nested historical ZIP/outer ZIP CRC and SHA verification. Existing
C01–C06/C05/C07/harness evidence is unchanged; prior prep archives preserved.
No target-machine pass, runtime compatibility or guaranteed3h completion is
claimed from offline fixtures. Disk/power loss can prevent final archive;
package recovery is documented. The private payload contains identifying raw
logs and must not be published unredacted. Independent review and actual AMD
machine checks remain gates, not completed work.
