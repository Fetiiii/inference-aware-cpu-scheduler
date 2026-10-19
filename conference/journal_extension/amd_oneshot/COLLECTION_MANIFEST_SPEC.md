# Evidence collection and manifest specification

All output is private evidence for `journal_extension_amd_01`, under a fresh `results/journal_extension_amd_01/` session. No historical result directory is reused as output. This document specifies required contents; it does not assert they already exist or passed verification.

## Historical collection first

Before preflight/requests, collect selected historical text evidence from the repository C03 result location and explicitly supplied `conference_c03` roots only. Never scan home directories. Preserve original bytes, source paths, nanosecond mtimes, byte counts and SHA256. Include available raw phase/detector/server logs, prompt evidence, build and topology/configuration records, and the related C03 metadata selected by archive.py. Record missing roots/files, exclusions and historical identity gaps explicitly. Collection must detect source mutation. Model weights, binaries, unrelated environment and symlink escapes are excluded, not silently copied.

Current prompt identity is a separate observation from historical prompt identity. A manifest entry for today's prompt is not historical proof. Missing historical identities block new requests even if the remaining archive is complete.

## Required session evidence

- Frozen RUN_PLAN, its canonical digest, reviewed package digest, author approval, persisted paths/configuration, generated complete-round schedule and source/code fingerprints.
- Historical originals/inventory and identity resolution, selected topology/masks, preflight results, binary/library/source/cache/patch/compiler identities, machine/power/thermal/interference snapshots and failures.
- Every logical slot and every attempt: run ID, cohort/role, round, order seed/index, arm, attempt index/status, timestamps, exact command/configuration, paths and reason codes. Skipped policy slots are explicit NOT_ELIGIBLE entries.
- Every raw server log, streaming token timing/output record, phase.json (proc samples, detector decisions, live markers, monitor diagnostics and actuation), run metadata, warmup/startup/request timings and safety samples, including failed/cancelled attempts.
- Frozen detector, training/validation ID/hash separation, zero-shot and calibrated reports, policy gate/reasons/endpoints/STATIC comparator, duration forecasts, retry ledger, cancellation/failure census and analysis products.
- Collector/runtime/analysis code and relevant frozen source evidence, archive exclusions, validation report and private-data notice. No model weights or executable/shared-library payloads; retain their identities instead.

These are semantic requirements; actual emitted filenames are defined by the implementation. Never invent a missing raw file or fill an unknown historical value from a default.

## Private archive integrity

`ARCHIVE_MANIFEST.json` uses `format: private-evidence-v1`, a files array with `path`, `sha256`, `bytes`, `mtime_ns`, and an explicit excluded census. Historical inventory additionally retains original source paths. Archive creation rejects collisions, path escapes, changed source content/mtime and changed member census. Preserve originals exactly; ZIP timestamp resolution does not replace manifest nanosecond mtimes.

Validate ZIP CRC, exact member census, every member size/SHA256, and the whole ZIP SHA256 in its separate `.sha256` sidecar. An incomplete or failed archive is not PASS. Existing output/archive targets must not be overwritten. Archive failed/cancelled sessions too. Public redaction uses a separate derived copy with its own manifest; it must not alter the private original archive.

The collaborator returns ONE final ZIP. The sidecar is a local convenience:
standalone verification checks CRC/member hashes and reports the computed ZIP
hash, explicitly indicating when no independent external ZIP hash was supplied.
`ARCHIVE_VALIDATION_METHOD.md` distinguishes content checks from the post-closure
verification result; no document claims to contain its own final archive hash.
The builder uses streaming I/O rather than buffering the complete session in RAM.
