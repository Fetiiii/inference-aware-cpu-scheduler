#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PINNED="571d0d540df04f25298d0e159e520d9fc62ed121"
SOURCE_DIR="${C04_WORKER_TAIL_SOURCE_DIR:-${REPO_ROOT}/.c04/llama.cpp-worker-tail-src}"
BUILD_DIR="${C04_WORKER_TAIL_BUILD_DIR:-${REPO_ROOT}/llama.cpp/build-c04-worker-tail}"
PHASE_PATCH="${REPO_ROOT}/conference/diagnostic/llama_cpp_phase_mark.patch"
C04_PATCH="${REPO_ROOT}/conference/diagnostic/c04_worker_tail.patch"

for flag_name in CFLAGS CXXFLAGS CPPFLAGS LDFLAGS; do
    if [[ -n "${!flag_name:-}" ]]; then
        echo "Unset ${flag_name} before the frozen C04 diagnostic build." >&2
        exit 2
    fi
done

for tool in git cmake patch tar c++ sha256sum; do
    command -v "${tool}" >/dev/null || { echo "Missing required build tool: ${tool}" >&2; exit 2; }
done
test "$(git -C "${REPO_ROOT}/llama.cpp" rev-parse HEAD)" = "${PINNED}" || {
    echo "llama.cpp submodule is not at pinned revision ${PINNED}" >&2; exit 2;
}
if [[ -e "${SOURCE_DIR}" || -e "${BUILD_DIR}" ]]; then
    echo "Refusing to overwrite existing source/build path:" >&2
    echo "  ${SOURCE_DIR}" >&2
    echo "  ${BUILD_DIR}" >&2
    exit 2
fi

mkdir -p "${SOURCE_DIR}" "${BUILD_DIR}"
git -C "${REPO_ROOT}/llama.cpp" archive "${PINNED}" | tar -x -C "${SOURCE_DIR}"
patch -d "${SOURCE_DIR}" -p1 --forward < "${PHASE_PATCH}"
patch -d "${SOURCE_DIR}" -p1 --forward < "${C04_PATCH}"

cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=ON \
    -DGGML_NATIVE=ON \
    -DGGML_CPU_REPACK=ON \
    -DGGML_LLAMAFILE=ON \
    -DGGML_OPENMP=ON \
    -DGGML_CUDA=OFF \
    -DGGML_VULKAN=OFF \
    -DLLAMA_BUILD_SERVER=ON \
    -DLLAMA_BUILD_COMMIT="${PINNED}" \
    -DLLAMA_BUILD_NUMBER="$(git -C "${REPO_ROOT}/llama.cpp" rev-list --count "${PINNED}")" \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build "${BUILD_DIR}" --target llama-server --parallel "${C04_BUILD_JOBS:-8}"
python3 "${REPO_ROOT}/conference/diagnostic/c04_build_manifest.py" \
    --source-dir "${SOURCE_DIR}" --build-dir "${BUILD_DIR}" \
    --phase-patch "${PHASE_PATCH}" --c04-patch "${C04_PATCH}" \
    --output "${BUILD_DIR}/c04_build_manifest.json"

echo "C04 worker-tail diagnostic built without changing the frozen C01/C02 build."
echo "Binary: ${BUILD_DIR}/bin/llama-server"
echo "Manifest: ${BUILD_DIR}/c04_build_manifest.json"
