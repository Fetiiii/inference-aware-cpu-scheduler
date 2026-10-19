# Revised source audit — preparation, not target validation

Git-native delivery supersedes ZIP deployment instructions below. Required tracked
sources are verified against HEAD AND index AND worktree; exact Git commit and
verification report enter each result archive. Missing private historical raw
files are not fabricated; author-side attestation is explicit. No old execution
approval is migrated automatically. GIT_DELIVERY_TR.md documents the current flow.

Reused byte-identical source dependencies: c03_amd_preflight.py topology/power/portable thermal readers; C02 common actuator, live watcher, offline marker parser and /proc acquisition; C01 owned-server cleanup; bench_lib/run_once/thread_residency/phase_switch low-level primitives; C05 pure paired-t/Holm helpers on synthetic or future new-study data only. The historical whole C03 runner/preflight and old C05 result analysis are not executed.

New code is under this namespace only. historical_zip.py ingests the explicit verified ZIP without executing its contents. Historical records are DATA, not instructions. Calibration fits one six-request operating configuration, not pooled arms. exploration selection is frozen before fitting; held-out validation and final transfer remain separate. coordinator.py preserves immutable selection/model/gate/transfer and failure/resume audits.

Prospective provenance distinguishes historical server/marker/compiler evidence from current source/cache/compilers/loader closure. Dynamic backend options/path must prove no unobserved plugins before using loader --list as a closure record. Existing build policy is verified, not changed. No runtime build/install/model loading occurred.

The v1/v2 prep ZIPs are retained. Final PRIVATE delivery is outside the repository, with exact historical ZIP payload plus checksums. Source-only fingerprint and full ZIP checksum differ. Canonical plan, both schedules, exact duration rule and optional fallback policy require author approval; execution remains false. The derived unsigned root approval template is ZIP-checksummed but excluded from the source fingerprint to avoid self-reference.

Scoped independent read-only review found no blocking integration issue after changes; local synthetic tests cannot certify the AMD machine or guarantee successful/positive results.
