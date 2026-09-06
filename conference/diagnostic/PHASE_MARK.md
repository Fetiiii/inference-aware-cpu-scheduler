# Frozen diagnostic PHASE_MARK instrumentation

The conference experiments use llama.cpp submodule commit
`571d0d540df04f25298d0e159e520d9fc62ed121` with
`llama_cpp_phase_mark.patch` applied.

The patch reconstructs and version-controls the diagnostic instrumentation
used by C01/C02. The historical build was made from a temporarily modified
`src/llama-context.cpp`; the source modification itself had not been committed.
The compiled historical object proves the following behavior:

- `CLOCK_MONOTONIC` is read with `clock_gettime`.
- The marker is emitted on the first `llama_context::graph_compute` call and
  whenever its `batched` argument changes.
- The exact record is
  `PHASE_MARK batched=%d t_mono_ns=%lld` and stderr is flushed immediately.
- The C01/C02/C03 boundary remains the first measured-request marker with
  `batched=0`: the first internally marked unbatched decode computation.

This is diagnostic instrumentation, not application phase input for the C03
external detector. The marker is used only to label signal samples offline.

## Historical Intel C01/C02 build record

The historical Intel diagnostic CMake cache recorded:

```text
CMAKE_BUILD_TYPE=Release
BUILD_SHARED_LIBS=ON
GGML_NATIVE=ON
GGML_OPENMP=ON
GGML_OPENMP_ENABLED=ON
LLAMA_BUILD_SERVER=ON
```

This is the frozen historical C01/C02 build record. It must not be retroactively
described as a non-native, AVX2-constrained build.

## C03 AMD protocol amendment

The official AMD HX 370 C03 pilot used a separately documented,
AVX2-constrained diagnostic build:

```text
CMAKE_BUILD_TYPE=Release
BUILD_SHARED_LIBS=ON
GGML_NATIVE=OFF
GGML_AVX=ON
GGML_AVX2=ON
GGML_FMA=ON
GGML_F16C=ON
GGML_AVX512=OFF
GGML_AVX512_VBMI=OFF
GGML_AVX512_VNNI=OFF
GGML_AVX512_BF16=OFF
GGML_OPENMP=ON
GGML_OPENMP_ENABLED=ON
LLAMA_BUILD_SERVER=ON
```

This C03 amendment reduces AMD-only AVX-512 vector-width capability as an
additional cross-vendor confound. It addresses only one vector-width
difference; it does not make the Intel and AMD architectures equivalent or
isolate core topology. The PHASE_MARK instrumentation and semantic boundary
remain unchanged. The AMD collaborator must build on the AMD machine rather
than copy the Intel-built binary.
