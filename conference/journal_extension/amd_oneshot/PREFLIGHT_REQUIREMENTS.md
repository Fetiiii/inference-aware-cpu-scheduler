# Preflight requirements — final preparation

No collaborator execution is approved. Read-only checks do not start a server or load a model.

| Gate | Requirement |
|---|---|
| Historical archive | Declared bundled payload or explicitly supplied absolute local ZIP; fixed SHA256, CRC, safe members; 12 complete reference sets and 2 environments; no recollection |
| Workload | Original model/prompt SHA, pinned source and historical runtime identities established by original environments |
| Existing runtime | Historical server and known marker library hashes match; native-OFF AVX2 cache/generated flags and exact phase patch verified |
| Prospective closure | Resolve/hash current transitive loader libraries, compilers and source/cache/patch before requests; distinguish newly measured hashes from historical identities |
| CPU | AMD HX370,12 physical/24 logical, clean4/8 classification; one allowed/online representative per physical core; no manual numbering assumptions |
| Machine | AC connected; readable temperatures; preflight<70C/run-start<80C/critical≥95C; memory≥12GiB, disk≥30GiB |
| State | Fixed readable governor/EPP/driver/power; sched_ext disabled/absent; no heavy concurrent workload |
| Output | Exclusive new session; preserved old raw evidence; source/workload/runtime identity guards on resume and between requests |
| Approval | Plan/source fingerprint, exact duration rule, BOTH schedules, fallback policy and10800s budget matched by separate author approval before any build/smoke |

Existing generated compile_commands, Ninja command metadata or flags.make may prove flags; preflight never configures/builds to create missing records. Tools must already exist. Missing/incompatible original runtime may defer ONLY its gate as RUNTIME_FALLBACK_PENDING. Separately approved optional fallback builds during run, not preflight; all full checks must subsequently PASS before smoke. See PROVENANCE_POLICY.md.

A full target preflight has NOT been performed on this development Intel machine. Historical identity PASS is not current AMD runtime/machine PASS.
