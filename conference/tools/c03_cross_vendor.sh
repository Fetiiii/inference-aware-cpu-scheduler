#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)
HELPER="${SCRIPT_DIR}/c03_amd_preflight.py"
RUNNER="${REPO_ROOT}/conference/experiments/c03_generality.py"
ANALYZER="${REPO_ROOT}/conference/analysis/c03_analyze.py"
PATCH_FILE="${REPO_ROOT}/conference/diagnostic/llama_cpp_phase_mark.patch"
LLAMA_DIR="${REPO_ROOT}/llama.cpp"
DEFAULT_SERVER="${LLAMA_DIR}/build-diag/bin/llama-server"
DEFAULT_MODEL="${REPO_ROOT}/models/Qwen3.5-9B-Q4_K_M.gguf"
DEFAULT_OUTDIR="${REPO_ROOT}/results/conference_c03"
PINNED_LLAMA_COMMIT="571d0d540df04f25298d0e159e520d9fc62ed121"

usage() {
    cat <<'EOF'
Usage:
  ./conference/tools/c03_cross_vendor.sh build-diag
  ./conference/tools/c03_cross_vendor.sh preflight [--model PATH] [--server-bin PATH] [--outdir PATH]
  ./conference/tools/c03_cross_vendor.sh smoke [--outdir PATH]
  ./conference/tools/c03_cross_vendor.sh pilot [--start-round ROUND] [--outdir PATH]
  ./conference/tools/c03_cross_vendor.sh analyze [--outdir PATH]

Debug inspection only (cannot authorize smoke):
  ./conference/tools/c03_cross_vendor.sh preflight --allow-non-hx370 [...]

No subcommand changes BIOS, SMT, affinity, scheduler, governor, EPP, boost,
amd_pstate, or power profile. preflight never starts inference.
EOF
}

require_commands() {
    local missing=()
    local command_name
    for command_name in "$@"; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            missing+=("${command_name}")
        fi
    done
    if ((${#missing[@]})); then
        echo "Missing required commands: ${missing[*]}" >&2
        echo "Install them using your distribution's package manager; this script never invokes sudo." >&2
        exit 2
    fi
}

build_diagnostic() {
    require_commands git cmake c++ make python3
    if [[ ! -e "${LLAMA_DIR}/.git" ]]; then
        echo "Initializing the pinned llama.cpp submodule..."
        git -C "${REPO_ROOT}" submodule update --init --recursive
    fi
    local observed_commit
    observed_commit=$(git -C "${LLAMA_DIR}" rev-parse HEAD)
    if [[ "${observed_commit}" != "${PINNED_LLAMA_COMMIT}" ]]; then
        echo "llama.cpp commit mismatch: expected ${PINNED_LLAMA_COMMIT}, observed ${observed_commit}" >&2
        exit 2
    fi

    if git -C "${LLAMA_DIR}" apply --reverse --check "${PATCH_FILE}" >/dev/null 2>&1; then
        echo "Frozen PHASE_MARK patch is already applied."
    else
        if [[ -n "$(git -C "${LLAMA_DIR}" status --porcelain --untracked-files=no)" ]]; then
            echo "llama.cpp has unexpected tracked modifications; refusing to apply the diagnostic patch." >&2
            git -C "${LLAMA_DIR}" status --short >&2
            exit 2
        fi
        git -C "${LLAMA_DIR}" apply --check "${PATCH_FILE}"
        git -C "${LLAMA_DIR}" apply "${PATCH_FILE}"
        echo "Applied frozen PHASE_MARK patch."
    fi
    source_state=$(git -C "${LLAMA_DIR}" status --porcelain --untracked-files=no)
    if [[ "${source_state}" != " M src/llama-context.cpp" ]]; then
        echo "llama.cpp does not contain exactly the frozen diagnostic source change:" >&2
        git -C "${LLAMA_DIR}" status --short >&2
        exit 2
    fi

    cmake -S "${LLAMA_DIR}" -B "${LLAMA_DIR}/build-diag" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON \
        -DGGML_NATIVE=OFF \
        -DGGML_AVX=ON \
        -DGGML_AVX2=ON \
        -DGGML_FMA=ON \
        -DGGML_F16C=ON \
        -DGGML_AVX512=OFF \
        -DGGML_AVX512_VBMI=OFF \
        -DGGML_AVX512_VNNI=OFF \
        -DGGML_AVX512_BF16=OFF \
        -DGGML_OPENMP=ON \
        -DLLAMA_BUILD_SERVER=ON
    cmake --build "${LLAMA_DIR}/build-diag" --target llama-server --parallel "${C03_BUILD_JOBS:-1}"

    if [[ ! -x "${DEFAULT_SERVER}" ]]; then
        echo "Diagnostic build completed without ${DEFAULT_SERVER}" >&2
        exit 2
    fi
    echo "Diagnostic llama-server built on this machine: ${DEFAULT_SERVER}"
    echo "Run preflight next; it will hash the binary, shared libraries, compiler, CMake cache, and marker capability."
}

COMMAND=${1:-}
if [[ -z "${COMMAND}" ]]; then
    usage
    exit 2
fi
shift
if [[ "${COMMAND}" == "-h" || "${COMMAND}" == "--help" ]]; then
    usage
    exit 0
fi

MODEL="${DEFAULT_MODEL}"
SERVER_BIN="${DEFAULT_SERVER}"
OUTDIR="${DEFAULT_OUTDIR}"
ALLOW_NON_HX370=0
MODEL_EXPLICIT=0
SERVER_EXPLICIT=0

while (($#)); do
    case "$1" in
        --model)
            [[ $# -ge 2 ]] || { echo "--model requires PATH" >&2; exit 2; }
            MODEL=$2
            MODEL_EXPLICIT=1
            shift 2
            ;;
        --server-bin)
            [[ $# -ge 2 ]] || { echo "--server-bin requires PATH" >&2; exit 2; }
            SERVER_BIN=$2
            SERVER_EXPLICIT=1
            shift 2
            ;;
        --outdir)
            [[ $# -ge 2 ]] || { echo "--outdir requires PATH" >&2; exit 2; }
            OUTDIR=$2
            shift 2
            ;;
        --start-round)
            [[ $# -ge 2 ]] || { echo "--start-round requires INT" >&2; exit 2; }
            START_ROUND=$2
            shift 2
            ;;
        --allow-non-hx370)
            ALLOW_NON_HX370=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "${COMMAND}" in
    build-diag)
        if ((MODEL_EXPLICIT || SERVER_EXPLICIT || ALLOW_NON_HX370)) || [[ "${OUTDIR}" != "${DEFAULT_OUTDIR}" ]]; then
            echo "build-diag does not accept preflight/smoke options" >&2
            exit 2
        fi
        build_diagnostic
        ;;
    preflight)
        require_commands python3 git lscpu uname
        preflight_args=(
            preflight
            --server-bin "${SERVER_BIN}"
            --model "${MODEL}"
            --outdir "${OUTDIR}"
        )
        if ((ALLOW_NON_HX370)); then
            preflight_args+=(--allow-non-hx370)
        fi
        python3 "${HELPER}" "${preflight_args[@]}"
        ;;
    smoke)
        if ((ALLOW_NON_HX370)); then
            echo "--allow-non-hx370 cannot be used with smoke" >&2
            exit 2
        fi
        if ((MODEL_EXPLICIT || SERVER_EXPLICIT)); then
            echo "smoke consumes the model and binary frozen by preflight; replacement options are forbidden" >&2
            exit 2
        fi
        require_commands python3 taskset
        python3 "${HELPER}" verify --outdir "${OUTDIR}"
        topology_env="${OUTDIR}/preflight/c03_topology.env"
        if [[ ! -f "${topology_env}" ]]; then
            echo "Successful preflight configuration missing: ${topology_env}" >&2
            exit 2
        fi
        # This file is generated by the helper from numeric CPU lists and
        # shell-quoted, preflight-hashed paths. Never consume a hand-written
        # topology file.
        # shellcheck disable=SC1090
        source "${topology_env}"
        python3 "${RUNNER}" \
            --path CROSS_VENDOR \
            --big-cpus "${C03_BIG_CPUS}" \
            --compact-cpus "${C03_COMPACT_CPUS}" \
            --threads-big "${C03_THREADS_BIG}" \
            --threads-all "${C03_THREADS_ALL}" \
            --server-bin "${C03_SERVER_BIN}" \
            --model "${C03_MODEL}" \
            --prompt "${REPO_ROOT}/harness/prompt_512.txt" \
            --rounds 2 \
            --order-seed 3304 \
            --detector-mode zero_shot \
            --interval-ms 20 \
            --hi 3000 \
            --lo 2100 \
            --k 2 \
            --ctx 2048 \
            --batch 2048 \
            --ubatch 512 \
            --n-predict 256 \
            --seed 42 \
            --port 8140 \
            --initial-cooldown 30 \
            --cooldown 30 \
            --outdir "${OUTDIR}" \
            --resume
        python3 "${ANALYZER}" --input "${OUTDIR}"
        python3 "${HELPER}" handoff --outdir "${OUTDIR}"
        ;;
    analyze)
        if ((MODEL_EXPLICIT || SERVER_EXPLICIT || ALLOW_NON_HX370)); then
            echo "analyze accepts only --outdir" >&2
            exit 2
        fi
        require_commands python3
        python3 "${ANALYZER}" --input "${OUTDIR}"
        python3 "${HELPER}" handoff --outdir "${OUTDIR}"
        ;;
    pilot)
        if ((ALLOW_NON_HX370)); then
            echo "--allow-non-hx370 cannot be used with pilot" >&2
            exit 2
        fi
        if ((MODEL_EXPLICIT || SERVER_EXPLICIT)); then
            echo "pilot consumes the model and binary frozen by preflight; replacement options are forbidden" >&2
            exit 2
        fi
        require_commands python3 taskset
        python3 "${HELPER}" verify --outdir "${OUTDIR}"
        topology_env="${OUTDIR}/preflight/c03_topology.env"
        if [[ ! -f "${topology_env}" ]]; then
            echo "Successful preflight configuration missing: ${topology_env}" >&2
            exit 2
        fi
        source "${topology_env}"
        python3 "${RUNNER}" \
            --path CROSS_VENDOR \
            --big-cpus "${C03_BIG_CPUS}" \
            --compact-cpus "${C03_COMPACT_CPUS}" \
            --threads-big "${C03_THREADS_BIG}" \
            --threads-all "${C03_THREADS_ALL}" \
            --server-bin "${C03_SERVER_BIN}" \
            --model "${C03_MODEL}" \
            --prompt "${REPO_ROOT}/harness/prompt_512.txt" \
            --rounds 6 \
            --start-round "${START_ROUND:-3}" \
            --order-seed 3304 \
            --detector-mode zero_shot \
            --interval-ms 20 \
            --hi 3000 \
            --lo 2100 \
            --k 2 \
            --ctx 2048 \
            --batch 2048 \
            --ubatch 512 \
            --n-predict 256 \
            --seed 42 \
            --port 8140 \
            --initial-cooldown 30 \
            --cooldown 30 \
            --outdir "${OUTDIR}" \
            --resume \
            --full-pilot-approved
        python3 "${ANALYZER}" --input "${OUTDIR}"
        python3 "${HELPER}" handoff --outdir "${OUTDIR}"
        ;;
    *)
        echo "Unknown subcommand: ${COMMAND}" >&2
        usage >&2
        exit 2
        ;;
esac
