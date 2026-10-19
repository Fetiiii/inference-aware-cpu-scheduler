# Historical versus prospective runtime identity

## Git-native distribution amendment (no scientific amendment)

New measurements require no private historical ZIP. Tracked HISTORICAL_IDENTITY.json
is a minimal author-side verification attestation, binding source archive SHA,
model/prompt/source/server/known marker identities and evidence counts. It is not
raw evidence and never reports collaborator-side raw verification as PASS.
The complete private archive remains with the authors for offline comparison.
`verify-history` is an optional author-side offline audit, never an execution gate
requiring collaborator raw collection. Existing original runtime/model checks below
are unchanged and consume the sanitized identity anchors.

Exact Git commit, relevant HEAD/index/worktree file identities, pinned submodule,
plan and both schedules/rule/policy are bound by a NEW detached Git-native approval.
ZIP-era package approvals are rejected. Private authorization is ignored by Git;
no authorization is created by this task. See GIT_DELIVERY_TR.md for trust model.
The frozen plan retains its historical ZIP path as archival metadata only; Git
execution never resolves that field. Internal legacy package_sha256 checksum
aliases are not ZIP-based authorization and do not replace the Git commit binding.

Historical evidence anchors the source commit, phase-marker semantics, original server hash and the marker-containing library hash. It did not record every shared library. Requiring historical hashes for unrecorded files is unachievable and is removed; inventing those hashes is forbidden.

Before new requests, the existing installation must pass:

1. Pinned source HEAD and exact reconstructed frozen phase patch; no unrelated source modifications.
2. Existing server SHA256 matches a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1. Every historically known marker-library hash must occur among currently resolved marker libraries.
3. Existing CMake cache/generated emitted flags implement the frozen AMD native-OFF AVX2 policy; no hidden native/AVX512 options.
4. Static PHASE_MARK capability and absence of C04 worker-tail instrumentation.
5. Require existing-cache GGML_BACKEND_DL=OFF, GGML_CPU_ALL_VARIANTS=OFF, empty GGML_BACKEND_DIR and no nonempty GGML_BACKEND_PATH. Otherwise return DYNAMIC_BACKEND_PROVENANCE_GAP: ELF listing cannot prove later dlopen identities. These are validation gates, NOT changes to historical build flags. Read ELF interpreter metadata and invoke the trusted anchored loader's --list (dependency listing only, no model/server request) to resolve transitive runtime libraries, including system libraries outside bin. Record resolution/environment and file SHA256; normalize ASLR addresses, not file identity.
6. Freeze the complete CURRENT source/cache/patch/binary/library/compiler-file fingerprint in preflight. Recheck it on each execution/resume segment and use per-request identity guards. Drift is a stop, not a re-freeze.

The preflight uses provenance_mode=prospective_current_identity_with_historical_anchor. Historical compiler capture is separate from hashes of currently installed compiler executables; the latter do not prove the build-time compiler. Existing original preflight records identify Ubuntu GCC15.2.0. Unrecorded historical library identities remain UNKNOWN. Current hashes are prospective new-study evidence, not reconstructed C03 facts.

## Separately approved prospective fallback

ORIGINAL_BINARY_MISSING, ORIGINAL_BINARY_MISMATCH, ORIGINAL_BINARY_INCOMPATIBLE or ORIGINAL_KNOWN_LIBRARY_MISMATCH yields RUNTIME_FALLBACK_PENDING only if ALL non-runtime preflight checks pass. This is not PASS, execution permission or permission to waive another gate. Arbitrary source/flags/metadata errors remain STOP conditions. Original failed reports are preserved.

The prepared fallback requires runtime_fallback_authorized=true in the immutable author approval, bound to the exact fallback policy, plan, source fingerprint, duration rule and BOTH schedules. The delivered template stays authorized=false and runtime_fallback_authorized=false. This preparation grants no execution permission.

The helper exports the pinned LOCAL git object into a new isolated workspace, applies the identical frozen PHASE_MARK patch and uses only existing local compilers/make/CMake/OpenMP. Native-OFF AVX2 policy is retained; UI/prebuilt-UI provisioning and FetchContent updates are disabled. No network fetch, package installation, sudo, system mutation or overwrite of historical builds. Missing prerequisites stop/archive. Compilation is capped at900s INSIDE the10800s session. Commands, compiler versions, flags and complete source/patch/cache/binary/transitive-library identities are recorded.

The fallback is a NEW prospective identity, not a reconstruction of the historical SHA. Old C03 performance is never pooled as identical-build repetitions. Historical Intel GGML_NATIVE=ON and historical AMD native-OFF/AVX2 records stay unchanged. All new arms use one selected runtime after a new full preflight. execution_config/preflight and runtime_selection supplement, never overwrite initial reports. Failed builds retain workspace/logs and failure archive; no automatic second build or alternative detector.
