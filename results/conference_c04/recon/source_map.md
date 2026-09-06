# C04A Q4_K decode source map

## Identification result

**High-confidence path identified for two-dimensional Q4_K matrix-vector
operations during unbatched decode:**

```text
GGML_OP_MUL_MAT
  -> CPU_REPACK extra-buffer dispatch
  -> tensor_traits<block_q4_K, 8, 8, GGML_TYPE_Q8_K>
  -> F32 activation quantization to Q8_K
  -> one-row decode selects GEMV
  -> ggml_gemv_q4_K_8x8_q8_K
```

This identifies the Q4_K multiplication path used by the frozen CPU build; it
does not claim that this one symbol accounts for all decode time. Q4_K_M is a
mixed quantization preset.

## Model evidence

A read-only parse of the frozen GGUF header found:

```text
GGUF version: 3
architecture: qwen35
general.file_type: 15 (LLAMA_FTYPE_MOSTLY_Q4_K_M)
tensors: 427
tensor type counts:
  F32 (0):   177
  Q8_0 (8):   48
  Q4_K (12): 132
  Q5_K (13):  48
  Q6_K (14):  22
Q4_K tensor ne[1] modulo 8: {0}
```

Thus the file really contains 132 Q4_K tensors, and every Q4_K tensor satisfies
the row-divisibility condition used by the x86 8x8 repack selector. Higher-bit
Q5_K/Q6_K tensors remain part of the Q4_K_M model and have their own kernels.

## Dispatch chain

1. **Extra buffer is enabled and preferred.**
   `llama.cpp/src/llama-model.cpp` defaults `use_extra_bufts=true` and puts
   CPU extra buffer types before the ordinary CPU buffer. The build defines
   `GGML_USE_CPU_REPACK`, so `ggml_backend_cpu_repack_buffer_type()` is offered.
   `select_weight_buft()` in `llama-model-loader.cpp` chooses the first buffer
   that supports the tensor's operation.

2. **Q4_K selects the AVX2 8x8 repack trait.**
   In `ggml-cpu/repack.cpp:4528-4604`,
   `ggml_repack_get_optimal_repack_type()` returns
   `tensor_traits<block_q4_K, 8, 8, GGML_TYPE_Q8_K>` when AVX2 is available
   and `ne[1]` is divisible by eight. Both facts are directly established for
   this build/model.

3. **The extra-buffer executor gets first refusal.**
   `ggml-cpu/ggml-cpu.c:1711-1721` calls
   `ggml_cpu_extra_compute_forward()` before the generic operation switch.
   `ggml-cpu/repack.cpp:4772-4816` accepts a two-dimensional CPU_REPACK
   `GGML_OP_MUL_MAT` with F32 activations. `traits.cpp` invokes the selected
   tensor trait's `compute_forward()`.

4. **Activations are converted to Q8_K.**
   The selected trait's parameter type is `GGML_TYPE_Q8_K`.
   `forward_mul_mat()` obtains that type's `from_float` implementation and
   converts the F32 activation rows into the per-operation work buffer. For
   the unbatched single-row case this resolves through the CPU type traits to
   `quantize_row_q8_K`.

5. **Unbatched decode selects GEMV.**
   `ggml-cpu/repack.cpp:4204-4250` uses GEMM only for more than three source
   rows. The internally marked decode graph is unbatched and its matrix
   operation has one activation row, so it takes the GEMV loop.

6. **Template specialization reaches the x86 symbol.**
   `ggml-cpu/repack.cpp:3990-3992` maps the selected Q4_K/8x8/Q8_K trait to
   `ggml_gemv_q4_K_8x8_q8_K`. Its architecture-specific implementation is in
   `ggml-cpu/arch/x86/repack.cpp:1464-1684`, guarded by `__AVX2__`, with a
   generic fallback only when that compile-time implementation is absent.

## Compiled-build verification

The dynamically loaded `libggml-cpu.so.0.17.0` and its source object are not
stripped. They export/contain:

- `ggml_gemv_q4_K_8x8_q8_K`
- `ggml_gemv_q4_K_8x8_q8_K_generic`
- `ggml_gemm_q4_K_8x8_q8_K`
- `ggml_vec_dot_q4_K_q8_K`
- `quantize_row_q8_K`
- `ggml_compute_forward_mul_mat`

The specialized symbol's disassembly contains the expected AVX2 body and does
not jump directly to its generic fallback. The x86 repack object is identical
between the Intel normal and diagnostic build trees.

## Confidence boundary

The route is high confidence because source selection conditions, GGUF tensor
shape/type metadata, generated flags, retained symbols, and compiled
instructions agree. No profiler or new inference run was used, so C04A does
not quantify what fraction of total decode time this path consumes and does
not make any causal performance claim.
