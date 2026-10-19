# TASK-JE-AMD-GIT — distribution candidate, not execution authorization

The approved private ZIP was checked read-only: CRC/member checksums passed and
the original implementation/plan matched the local preparation state. Private
raw files and its real ZIP-era execution approval are not imported into Git.

## Distribution-only changes

- Git checkout is the delivery unit. Exact commit, pinned submodule, HEAD/index/
  worktree contents and required tracked-source inventory are verified. A source
  snapshot and Git verification report are included in each new result archive.
- New detached Git approval schema binds exact commit/source, plan, FULL and
  STATIC_SIGNAL schedules, duration rule and fallback policy. Old ZIP approvals
  are rejected, even when the scientific plan hash is unchanged. No real
  authorization is tracked or generated; collaborator --confirm is still required.
- Minimal public historical identity attestation records the authors' offline
  verification of12 original AMD runs/2 environments and known model/prompt/
  source/binary/marker hashes. It explicitly says private raw is not bundled or
  verified on the collaborator. Offline author comparison remains a separate
  responsibility; private ZIP absence cannot be misreported as raw evidence PASS.
- Canonical model/runtime paths are discoverable, with explicit path overrides.
  Missing model is a hard gate. Missing/incompatible original runtime can defer
  only its gate; separately approved offline fallback still uses the same pinned
  source/PHASE_MARK/AVX2 policy and900s cap within10800s. No new build occurred here.
- Dedicated outputs, authorization and fallback workspace are ignored. Staging
  is explicit-allowlist only. Publication checks inspect index blobs and newly
  introduced history, including sensitive content later deleted from a commit.

## Unchanged science

RUN_PLAN.json remains byte-identical to the approved source. No changes to
runtime.py, calibration.py or analysis.py. Four static configurations, worker
budgets,4/8/6/6/24/conditional24 cohorts, six-request single-mask calibration and
independent validation, outcome-blind FULL72/STATIC_SIGNAL48/STOP, zero-shot
reference, endpoint/thermal gates, no-trigger handling, retry/resume rules and
twelve-test Holm family remain frozen. Old C01–C06 evidence is untouched.

## Trust and remaining gates

GIT_DELIVERY_TR.md is the current operator guide; older ZIP-preparation documents
are historical context only. The approved ZIP's authorization is not valid for
this changed source identity. A new exact-commit author review and detached
authorization are necessary before deployment, even if scientific parameters are
unchanged. JSON hash binding is not a cryptographic signature; author approval
must arrive through a trusted channel.

Validation uses only synthetic/local Git fixtures. A real AMD machine preflight,
compiler/dependency availability, runtime compatibility and duration remain
unverified here. No inference, runtime build, install or system mutation occurred.
No remote push or collaborator contact is part of this checkpoint.
