# C04A Intel build provenance

## Scope and status

This record identifies the Intel binaries and objects used by C01/C02. It is
not the AMD C03 build record. The evidence below is sufficient to reproduce
the source revision, compiler, CMake policy, generated kernel compile command,
binary identity, model identity, and diagnostic marker semantics.

**Provenance status: ESTABLISHED.** The exact compiler expansion hidden behind
`-march=native` was not persisted as a separate option list. The emitted
compile command and the original compiled object are present, however, so the
instruction behavior of the Q4_K kernel can be inspected directly without
guessing that expansion.

## Frozen source and runtime identities

| item | recorded identity |
|---|---|
| llama.cpp revision | `571d0d540df04f25298d0e159e520d9fc62ed121` |
| diagnostic server | `llama.cpp/build-diag/bin/llama-server` |
| diagnostic server SHA-256 | `4178b9d5b4f0d7abe9117d96bb2242d63519e3004f88e7e2ca7a4ce585c8b1bd` |
| diagnostic server GNU build ID | `f3482a99ceaa105b9623504c67ed589d6bb176d0` |
| diagnostic `libggml-cpu.so.0.17.0` SHA-256 | `57ccdee80ec2d9836dd856149a046d5bf354786ed45aafa1d2be82a375a56789` |
| diagnostic CPU library GNU build ID | `c0941e7f7c9aa3d66b79aadd8c46a3c423915581` |
| non-marker C01 performance server | `llama.cpp/build/bin/llama-server` |
| non-marker server SHA-256 | `4853e2bf4d107565a7f14144d8d828948bd39bb13391ab23bf5d629a573d30af` |
| model | `models/Qwen3.5-9B-Q4_K_M.gguf` |
| model SHA-256 | `03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8` |
| model size | `5680522464` bytes |

C01 characterization and every C02 arm record the diagnostic server hash
above. C01's low-overhead performance path records the non-marker server.
Both Intel build trees use the same source revision and CMake policy, and the
compiled x86 Q4_K repack object is byte-identical in them:

```text
SHA-256 1e8b20e158421df6084fd06d9ad4bf63ca9b9aa31abaef322177104e13f47069
cmp result: identical
```

Consequently, inspecting the retained diagnostic Q4_K kernel object does not
substitute the AMD C03 build and does not introduce a C01 performance-vs-C02
binary difference in that kernel.

## Compiler and CMake record

- CMake cache version: 4.4.0; generator: Unix Makefiles.
- Compiler: `/usr/bin/c++`, GNU 16.1.1.
- ELF `.comment`: `GCC: (GNU) 16.1.1 20260625`.
- `llama-server --version`: `version: 1 (571d0d5)`, built with GNU 16.1.1
  for Linux x86_64.
- Build type: Release; shared libraries enabled.
- `GGML_NATIVE=ON` (the frozen historical Intel C01/C02 policy).
- `GGML_CPU_REPACK=ON`, `GGML_LLAMAFILE=ON`, `GGML_OPENMP=ON`, and
  `GGML_OPENMP_ENABLED=ON`.
- `LLAMA_BUILD_SERVER=ON`.
- Generated x86 repack command includes `-O3 -DNDEBUG -march=native
  -fopenmp` and the `GGML_USE_CPU_REPACK`, `GGML_USE_LLAMAFILE`, and
  `GGML_USE_OPENMP` definitions.

The cache's standalone `GGML_AVX*` and `GGML_FMA` options are `OFF`. They do
not describe an AVX-disabled binary when `GGML_NATIVE=ON`: the generated CPU
translation-unit command uses `-march=native`, and the retained object directly
contains AVX2/FMA instructions. See `build_flags.txt` and
`disassembly_excerpt.txt`.

The build host is an Intel Core i7-14650HX. Its recorded/current exposed ISA
includes AVX, AVX2, F16C, and FMA but not AVX-512. A read-only compiler query on
the same host currently resolves `-march=native`/`-mtune=native` to
`alderlake`; this current query is corroborative only, not a replacement for
the original object.

## Diagnostic PHASE_MARK provenance

The version-controlled reconstruction is
`conference/diagnostic/llama_cpp_phase_mark.patch`. The retained
`llama-context.cpp.o` contains both the exact marker string and the local
state symbols.

The patch:

- calls `clock_gettime(CLOCK_MONOTONIC, ...)`;
- emits on the first `llama_context::graph_compute` call and whenever its
  `batched` argument changes;
- writes `PHASE_MARK batched=%d t_mono_ns=%lld` to stderr and immediately
  flushes it.

The experimental boundary remains the first measured-request marker with
`batched=0`, meaning the first internally marked unbatched decode computation.
This diagnostic record does not change the Q4_K CPU kernel object.

## Privacy and provenance boundaries

Machine-local repository and home paths are represented as `<REPO_ROOT>` and
`<MODEL_ROOT>` in C04A artifacts. Scientifically relevant hashes, revisions,
compiler versions, kernel/build IDs, and CPU identity are retained.
