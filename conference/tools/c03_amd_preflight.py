#!/usr/bin/env python3
"""Fail-closed AMD Ryzen AI 9 HX 370 bootstrap checks for TASK-C03.

This helper only reads machine state and writes experiment metadata.  It never
starts inference, changes affinity, or mutates a system setting.  The shell
launcher invokes the existing C03 runner only after this helper has persisted
and revalidated a PASS configuration.
"""

import argparse
import glob
import hashlib
import json
import math
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINNED_LLAMA_COMMIT = "571d0d540df04f25298d0e159e520d9fc62ed121"
MARKER = b"PHASE_MARK batched=%d t_mono_ns=%lld"
EXPECTED_MODEL_NAME = "Qwen3.5-9B-Q4_K_M.gguf"
EXPECTED_BIG_CORES = 4
EXPECTED_COMPACT_CORES = 8
EXPECTED_PHYSICAL_CORES = 12
EXPECTED_LOGICAL_CPUS = 24


class PreflightError(RuntimeError):
    pass


class AmbiguousClassification(PreflightError):
    def __init__(self, message, cores=None):
        super().__init__(message)
        self.cores = cores or []


def utc_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_int(path):
    value = read_text(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_cpu_list(value):
    cpus = set()
    if not value:
        return []
    for raw in value.split(","):
        part = raw.strip()
        if not part:
            raise PreflightError(f"invalid empty CPU-list component: {value!r}")
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                raise PreflightError(f"invalid CPU range: {part}")
            start, end = (int(item) for item in pieces)
            if start < 0 or end < start:
                raise PreflightError(f"invalid CPU range: {part}")
            cpus.update(range(start, end + 1))
        else:
            cpu = int(part)
            if cpu < 0:
                raise PreflightError("CPU IDs must be non-negative")
            cpus.add(cpu)
    return sorted(cpus)


def cpu_list_text(cpus):
    return ",".join(str(cpu) for cpu in sorted(cpus))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path):
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "path": str(path),
        "resolved_path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(resolved),
    }


def validate_model_identity(path):
    model_path = Path(path).resolve()
    if not model_path.is_file():
        raise PreflightError(
            f"required model missing: {model_path}; provide --model PATH. "
            "No download URL is guessed by this repository."
        )
    if model_path.name != EXPECTED_MODEL_NAME:
        raise PreflightError(
            f"model substitution forbidden: expected {EXPECTED_MODEL_NAME}, "
            f"observed {model_path.name}"
        )
    return file_identity(model_path)


def run_readonly(command, cwd=None):
    try:
        result = subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=False,
        )
    except OSError as exc:
        return {"command": command, "returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.rstrip(),
        "stderr": result.stderr.rstrip(),
    }


def git_output(args, cwd=ROOT):
    result = run_readonly(["git", *args], cwd=cwd)
    if result["returncode"] != 0:
        raise PreflightError(
            f"git {' '.join(args)} failed: {result['stderr'] or result['stdout']}"
        )
    return result["stdout"].strip()


def cpu_identity(cpuinfo_path="/proc/cpuinfo"):
    text = read_text(cpuinfo_path) or ""
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key in ("vendor_id", "model name", "Hardware") and key not in values:
            values[key] = value
    return {
        "vendor_id": values.get("vendor_id"),
        "model_name": values.get("model name") or values.get("Hardware"),
        "architecture": platform.machine(),
    }


def collect_cpu_records(sysfs_cpu_root="/sys/devices/system/cpu", allowed=None):
    root = Path(sysfs_cpu_root)
    online = parse_cpu_list(read_text(root / "online") or "")
    if not online:
        online = sorted(
            int(path.name[3:]) for path in root.glob("cpu[0-9]*")
            if path.name[3:].isdigit()
        )
    allowed_set = set(allowed if allowed is not None else os.sched_getaffinity(0))
    records = []
    for cpu in online:
        cpu_root = root / f"cpu{cpu}"
        topology = cpu_root / "topology"
        siblings = parse_cpu_list(
            read_text(topology / "thread_siblings_list") or str(cpu)
        )
        record = {
            "cpu": cpu,
            "online": True,
            "allowed": cpu in allowed_set,
            "package_id": read_int(topology / "physical_package_id"),
            "core_id": read_int(topology / "core_id"),
            "thread_siblings": siblings,
            "highest_perf": first_int(
                cpu_root / "acpi_cppc" / "highest_perf",
                cpu_root / "cpufreq" / "amd_pstate_highest_perf",
            ),
            "nominal_perf": first_int(
                cpu_root / "acpi_cppc" / "nominal_perf",
                cpu_root / "cpufreq" / "amd_pstate_nominal_perf",
            ),
            "lowest_nonlinear_perf": read_int(
                cpu_root / "acpi_cppc" / "lowest_nonlinear_perf"
            ),
            "max_freq_khz": first_int(
                cpu_root / "cpufreq" / "cpuinfo_max_freq",
                cpu_root / "cpufreq" / "scaling_max_freq",
            ),
            "cpu_capacity": read_int(cpu_root / "cpu_capacity"),
            "core_type": read_text(topology / "core_type"),
            "scaling_driver": read_text(cpu_root / "cpufreq" / "scaling_driver"),
            "governor": read_text(cpu_root / "cpufreq" / "scaling_governor"),
            "epp": read_text(cpu_root / "cpufreq" / "energy_performance_preference"),
        }
        records.append(record)
    return records


def first_int(*paths):
    for path in paths:
        value = read_int(path)
        if value is not None:
            return value
    return None


def group_physical_cores(records):
    """Group logical CPUs by package/core IDs and validate SMT declarations."""
    groups = {}
    record_by_cpu = {record["cpu"]: record for record in records}
    for record in records:
        if record.get("package_id") is None or record.get("core_id") is None:
            raise PreflightError(
                f"CPU {record['cpu']} lacks physical_package_id/core_id"
            )
        key = (record["package_id"], record["core_id"])
        groups.setdefault(key, []).append(record)
    cores = []
    conflicts = []
    for key, members in sorted(groups.items()):
        member_cpus = sorted(member["cpu"] for member in members)
        declared = [sorted(member.get("thread_siblings", [])) for member in members]
        if any(siblings != member_cpus for siblings in declared):
            raise PreflightError(
                f"inconsistent SMT sibling declaration for physical core {key}: "
                f"members={member_cpus}, declared={declared}"
            )
        values = {}
        metric_conflicts = {}
        for field in (
            "highest_perf", "nominal_perf", "max_freq_khz", "cpu_capacity",
            "core_type",
        ):
            observed = sorted({member.get(field) for member in members if member.get(field) is not None})
            if len(observed) > 1:
                metric_conflicts[field] = observed
                conflicts.append(f"physical core {key} has conflicting {field}: {observed}")
                values[field] = None
            else:
                values[field] = observed[0] if observed else None
        eligible = [
            cpu for cpu in member_cpus
            if record_by_cpu[cpu].get("online") and record_by_cpu[cpu].get("allowed")
        ]
        if not eligible:
            raise PreflightError(
                f"physical core {key} has no online, allowed logical sibling"
            )
        cores.append({
            "package_id": key[0],
            "core_id": key[1],
            "sibling_cpus": member_cpus,
            "representative_cpu": min(eligible),
            "metric_conflicts": metric_conflicts,
            **values,
        })
    if conflicts:
        raise AmbiguousClassification("; ".join(conflicts), cores)
    return cores


def split_candidate(cores, field, minimum_relative_gap=0.0):
    if len(cores) != EXPECTED_PHYSICAL_CORES:
        return None
    if any(core.get(field) is None for core in cores):
        return None
    ordered = sorted(cores, key=lambda core: (core[field], core["core_id"], core["package_id"]))
    compact = ordered[:EXPECTED_COMPACT_CORES]
    big = ordered[EXPECTED_COMPACT_CORES:]
    low_edge = compact[-1][field]
    high_edge = big[0][field]
    if high_edge <= low_edge:
        return None
    relative_gap = (high_edge - low_edge) / abs(low_edge) if low_edge else math.inf
    if relative_gap < minimum_relative_gap:
        return None
    return {
        "field": field,
        "lower_edge": low_edge,
        "upper_edge": high_edge,
        "relative_gap": relative_gap,
        "big_keys": [(core["package_id"], core["core_id"]) for core in big],
        "compact_keys": [(core["package_id"], core["core_id"]) for core in compact],
    }


def classify_physical_cores(cores):
    """Select a clean 4/8 split, preferring CPPC over static max frequency."""
    candidates = []
    for field, gap in (
        ("highest_perf", 0.0),
        ("max_freq_khz", 0.05),
        ("cpu_capacity", 0.0),
    ):
        candidate = split_candidate(cores, field, gap)
        if candidate:
            candidates.append(candidate)
    if not candidates:
        raise AmbiguousClassification(
            "no hardware-exposed metric provides a clean 4-big/8-compact split",
            cores,
        )
    selected = candidates[0]
    selected_big = set(selected["big_keys"])
    conflicts = [
        candidate for candidate in candidates[1:]
        if set(candidate["big_keys"]) != selected_big
    ]
    if conflicts:
        raise AmbiguousClassification(
            "hardware-exposed performance-class metrics disagree on core membership",
            cores,
        )
    by_key = {(core["package_id"], core["core_id"]): core for core in cores}
    big = [by_key[key] for key in selected["big_keys"]]
    compact = [by_key[key] for key in selected["compact_keys"]]
    return {
        "classification_source": selected["field"],
        "classification_evidence": candidates,
        "big_cores": sorted(big, key=lambda core: core["representative_cpu"]),
        "compact_cores": sorted(compact, key=lambda core: core["representative_cpu"]),
    }


def validate_target(identity, records, cores, selection, allow_non_hx370=False):
    if identity.get("vendor_id") != "AuthenticAMD":
        raise PreflightError(
            f"CROSS_VENDOR requires AMD vendor_id=AuthenticAMD; observed {identity.get('vendor_id')!r}"
        )
    if "Intel" in (identity.get("model_name") or ""):
        raise PreflightError("Intel target is forbidden in CROSS_VENDOR mode")
    if not allow_non_hx370:
        if "HX 370" not in (identity.get("model_name") or ""):
            raise PreflightError(
                f"expected Ryzen AI 9 HX 370 model; observed {identity.get('model_name')!r}"
            )
        if len(records) != EXPECTED_LOGICAL_CPUS or len(cores) != EXPECTED_PHYSICAL_CORES:
            raise PreflightError(
                "expected HX 370 topology 12 physical/24 logical; observed "
                f"{len(cores)} physical/{len(records)} logical"
            )
    if len(selection["big_cores"]) != EXPECTED_BIG_CORES:
        raise PreflightError("classification did not select exactly four big cores")
    if len(selection["compact_cores"]) != EXPECTED_COMPACT_CORES:
        raise PreflightError("classification did not select exactly eight compact cores")
    all_cores = selection["big_cores"] + selection["compact_cores"]
    keys = [(core["package_id"], core["core_id"]) for core in all_cores]
    if len(keys) != len(set(keys)):
        raise PreflightError("selected physical-core membership overlaps")
    masks = [core["representative_cpu"] for core in all_cores]
    if len(masks) != len(set(masks)):
        raise PreflightError("sibling duplication in selected representatives")
    selected_cpu_set = set(masks)
    record_by_cpu = {record["cpu"]: record for record in records}
    for cpu in selected_cpu_set:
        record = record_by_cpu.get(cpu)
        if not record or not record.get("online") or not record.get("allowed"):
            raise PreflightError(f"selected CPU {cpu} is not online and allowed")
    if not allow_non_hx370 and any(len(core["sibling_cpus"]) != 2 for core in cores):
        raise PreflightError(
            "SMT must remain enabled: expected two online siblings per physical core"
        )


def validate_masks(big_cpus, compact_cpus, cores):
    big = list(big_cpus)
    compact = list(compact_cpus)
    if set(big) & set(compact):
        raise PreflightError("BIG and COMPACT masks overlap")
    if len(big) != EXPECTED_BIG_CORES or len(compact) != EXPECTED_COMPACT_CORES:
        raise PreflightError("masks must contain exactly 4 BIG and 8 COMPACT representatives")
    cpu_to_key = {}
    for core in cores:
        key = (core["package_id"], core["core_id"])
        for cpu in core["sibling_cpus"]:
            cpu_to_key[cpu] = key
    selected_keys = [cpu_to_key.get(cpu) for cpu in big + compact]
    if None in selected_keys or len(selected_keys) != len(set(selected_keys)):
        raise PreflightError("selected masks duplicate an SMT sibling or unknown CPU")


def scan_phase_mark_capability(server_bin):
    binary = Path(server_bin).resolve()
    candidates = [binary]
    candidates.extend(sorted(binary.parent.glob("*.so*")))
    inspected = []
    matches = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        inspected.append(str(resolved))
        try:
            data = resolved.read_bytes()
        except OSError:
            continue
        if MARKER in data:
            matches.append(str(resolved))
    if not matches:
        raise PreflightError(
            "selected diagnostic runtime does not contain the exact PHASE_MARK "
            "format in the executable or sibling shared libraries"
        )
    return {
        "supported": True,
        "format": MARKER.decode(),
        "matches": matches,
        "matched_file_identities": [file_identity(path) for path in matches],
        "inspected": inspected,
    }


def parse_cmake_cache(path):
    values = {}
    text = read_text(path) or ""
    for line in text.splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        key_type, value = line.split("=", 1)
        key = key_type.split(":", 1)[0]
        values[key] = value
    return values


def verify_diagnostic_build(repo_root, server_bin):
    repo_root = Path(repo_root).resolve()
    llama = repo_root / "llama.cpp"
    patch = repo_root / "conference" / "diagnostic" / "llama_cpp_phase_mark.patch"
    commit = git_output(["rev-parse", "HEAD"], cwd=llama)
    if commit != PINNED_LLAMA_COMMIT:
        raise PreflightError(
            f"llama.cpp commit mismatch: expected {PINNED_LLAMA_COMMIT}, observed {commit}"
        )
    reverse = run_readonly(["git", "apply", "--reverse", "--check", str(patch)], cwd=llama)
    if reverse["returncode"] != 0:
        raise PreflightError(
            "frozen diagnostic patch is not applied to the pinned llama.cpp source; "
            "run ./conference/tools/c03_cross_vendor.sh build-diag"
        )
    changed = [
        line.strip()
        for line in git_output(
            ["status", "--porcelain", "--untracked-files=no"], cwd=llama
        ).splitlines()
        if line.strip()
    ]
    if changed != ["M src/llama-context.cpp"]:
        raise PreflightError(
            "llama.cpp source differs from the expected diagnostic patch state: "
            f"{changed}"
        )
    binary = Path(server_bin).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise PreflightError(f"diagnostic llama-server missing or not executable: {binary}")
    cache_path = binary.parent.parent / "CMakeCache.txt"
    cache = parse_cmake_cache(cache_path)
    expected = {
        "CMAKE_BUILD_TYPE": "Release",
        "BUILD_SHARED_LIBS": "ON",
        "GGML_NATIVE": "OFF",
        "GGML_AVX2": "ON",
        "GGML_AVX512": "OFF",
        "GGML_OPENMP": "ON",
        "GGML_OPENMP_ENABLED": "ON",
        "LLAMA_BUILD_SERVER": "ON",
    }
    mismatches = {
        key: {"expected": value, "observed": cache.get(key)}
        for key, value in expected.items() if cache.get(key) != value
    }
    if Path(cache.get("CMAKE_HOME_DIRECTORY", "")).resolve() != llama.resolve():
        mismatches["CMAKE_HOME_DIRECTORY"] = {
            "expected": str(llama.resolve()),
            "observed": cache.get("CMAKE_HOME_DIRECTORY"),
        }
    if mismatches:
        raise PreflightError(f"diagnostic CMake configuration mismatch: {mismatches}")
    capability = scan_phase_mark_capability(binary)
    patched_source = llama / "src" / "llama-context.cpp"
    source_mtime = patched_source.stat().st_mtime_ns
    built_files = [binary, *[Path(item["resolved_path"]) for item in capability["matched_file_identities"]]]
    older = [str(path) for path in built_files if path.stat().st_mtime_ns < source_mtime]
    if older:
        raise PreflightError(
            "diagnostic outputs predate the applied frozen source patch: "
            + ", ".join(older)
        )
    compiler = cache.get("CMAKE_CXX_COMPILER")
    compiler_identity = run_readonly([compiler, "--version"]) if compiler else None
    return {
        "llama_cpp_commit": commit,
        "patch_path": str(patch.relative_to(repo_root)),
        "patch_sha256": sha256_file(patch),
        "cmake_cache": str(cache_path),
        "frozen_cmake_options": expected,
        "compiler": compiler,
        "compiler_identity": compiler_identity,
        "binary_file_identity": (
            run_readonly(["file", str(binary)]) if shutil.which("file") else None
        ),
        "patched_source_mtime_ns": source_mtime,
        "server_binary": file_identity(binary),
        "phase_mark_capability": capability,
    }


def collect_power_state(records):
    cpu_root = Path("/sys/devices/system/cpu")
    governors = sorted({record["governor"] for record in records if record.get("governor")})
    epps = sorted({record["epp"] for record in records if record.get("epp")})
    drivers = sorted({record["scaling_driver"] for record in records if record.get("scaling_driver")})
    ac_paths = sorted(
        glob.glob("/sys/class/power_supply/AC*/online")
        + glob.glob("/sys/class/power_supply/ADP*/online")
    )
    ac_values = {path: read_text(path) for path in ac_paths}
    boost = {
        str(path): read_text(path)
        for path in (
            cpu_root / "cpufreq" / "boost",
            cpu_root / "amd_pstate" / "cpb_boost",
        ) if Path(path).exists()
    }
    profile = read_text("/sys/firmware/acpi/platform_profile")
    ppd = run_readonly(["powerprofilesctl", "get"]) if shutil.which("powerprofilesctl") else None
    warnings = []
    if ac_values and not any(value == "1" for value in ac_values.values()):
        warnings.append("machine appears to be on battery; run plugged in")
    if len(governors) > 1:
        warnings.append(f"governor differs across CPUs: {governors}")
    if len(epps) > 1:
        warnings.append(f"EPP differs across CPUs: {epps}")
    if any(value == "0" for value in boost.values()):
        warnings.append(f"boost appears disabled: {boost}")
    return {
        "scaling_drivers": drivers,
        "governors": governors,
        "epp": epps,
        "amd_pstate_status": read_text(cpu_root / "amd_pstate" / "status"),
        "amd_pstate_prefcore": (
            read_text(cpu_root / "amd_pstate" / "prefcore")
            or read_text("/sys/module/amd_pstate/parameters/prefcore")
        ),
        "boost": boost,
        "platform_profile": profile,
        "powerprofilesctl": ppd,
        "ac_online": ac_values,
        "warnings": warnings,
    }


def collect_thermal_state():
    sensors = {}
    for input_path in sorted(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")):
        raw = read_int(input_path)
        if raw is None:
            continue
        path = Path(input_path)
        label = read_text(str(path).replace("_input", "_label")) or path.name
        name = read_text(path.parent / "name") or path.parent.name
        sensors[f"{name}:{label}:{input_path}"] = raw / 1000.0
    package_like = [
        value for key, value in sensors.items()
        if any(term in key.lower() for term in ("tctl", "tdie", "package"))
    ]
    selected = max(package_like) if package_like else (max(sensors.values()) if sensors else None)
    warnings = []
    if selected is not None and selected >= 80.0:
        warnings.append(f"preflight temperature is already high: {selected:.1f} C")
    return {"selected_c": selected, "sensors_c": sensors, "warnings": warnings}


def core_table(cores):
    fields = (
        "package_id", "core_id", "representative_cpu", "sibling_cpus",
        "highest_perf", "nominal_perf", "max_freq_khz", "cpu_capacity",
        "core_type",
        "metric_conflicts",
    )
    return [{field: core.get(field) for field in fields} for core in cores]


def markdown_table(cores):
    lines = [
        "| package | core_id | representative | siblings | highest_perf | nominal_perf | max_freq_khz | capacity | core_type | conflicts |",
        "|---:|---:|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for core in cores:
        lines.append(
            f"| {core.get('package_id')} | {core.get('core_id')} | "
            f"{core.get('representative_cpu')} | {cpu_list_text(core.get('sibling_cpus', []))} | "
            f"{core.get('highest_perf')} | {core.get('nominal_perf')} | "
            f"{core.get('max_freq_khz')} | {core.get('cpu_capacity')} | "
            f"{core.get('core_type')} | {core.get('metric_conflicts') or ''} |"
        )
    return lines


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def redact_repo_root(path):
    resolved = str(Path(path).resolve())
    root_str = str(ROOT.resolve())
    if resolved == root_str or resolved.startswith(root_str + "/"):
        return f"<REPO_ROOT>{resolved[len(root_str):]}"
    return resolved


def topology_env_text(selected, build, model):
    server_bin = redact_repo_root(build["server_binary"]["resolved_path"])
    model_path = redact_repo_root(model["resolved_path"])
    return "\n".join((
        f"C03_BIG_CPUS={cpu_list_text(selected['big_cpus'])}",
        f"C03_COMPACT_CPUS={cpu_list_text(selected['compact_cpus'])}",
        "C03_THREADS_BIG=4",
        "C03_THREADS_ALL=12",
        f"C03_SERVER_BIN={shlex.quote(server_bin)}",
        f"C03_MODEL={shlex.quote(model_path)}",
        "",
    ))


def archive_current_preflight(preflight_dir):
    preflight_dir = Path(preflight_dir)
    names = (
        "topology.json", "environment.txt", "selected_topology.json",
        "preflight_summary.md", "preflight_status.json", "c03_topology.env",
    )
    existing = [preflight_dir / name for name in names if (preflight_dir / name).exists()]
    if not existing:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"_{time.time_ns()}"
    history = preflight_dir / "history" / stamp
    history.mkdir(parents=True, exist_ok=False)
    for source in existing:
        shutil.copy2(source, history / source.name)
    return str(history)


def environment_text(identity, records, allowed, commands, power, thermal, git_meta):
    lines = [
        f"captured_at={utc_timestamp()}",
        f"kernel={platform.uname()}",
        f"cpu_identity={json.dumps(identity, sort_keys=True)}",
        f"online_cpus={cpu_list_text(record['cpu'] for record in records)}",
        f"allowed_affinity={cpu_list_text(allowed)}",
        f"git={json.dumps(git_meta, sort_keys=True)}",
        f"power={json.dumps(power, sort_keys=True)}",
        f"thermal={json.dumps(thermal, sort_keys=True)}",
    ]
    for name, result in commands.items():
        lines += ["", f"===== {name} =====", result.get("stdout", "")]
        if result.get("stderr"):
            lines += ["--- stderr ---", result["stderr"]]
    return "\n".join(lines) + "\n"


def check_existing_plan(outdir, selected):
    plan_path = Path(outdir) / "raw" / "plan.json"
    if not plan_path.exists():
        unexpected = [
            path for path in Path(outdir).iterdir()
            if path.name != "preflight"
        ] if Path(outdir).exists() else []
        if unexpected:
            raise PreflightError(
                "result directory contains data but no compatible C03 plan: "
                + ", ".join(str(path) for path in unexpected)
            )
        return
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"existing C03 plan is unreadable: {exc}")
    expected_topology = {
        "big": selected["big_cpus"],
        "compact": selected["compact_cpus"],
        "all": selected["big_cpus"] + selected["compact_cpus"],
    }
    expected_detector = {
        "mode": "zero_shot", "interval_ms": 20.0, "hi": 3000.0,
        "lo": 2100.0, "k": 2,
        "frozen_intel_parameters_unchanged": True,
        "recalibration_label": "",
    }
    expected_arms = {
        "BIG_ONLY": {
            "threads": 4, "threads_batch": 4,
            "cpu_mask": selected["big_cpus"],
        },
        "ALL_CORES": {
            "threads": 12, "threads_batch": 12,
            "cpu_mask": selected["big_cpus"] + selected["compact_cpus"],
        },
    }
    if (
        plan.get("task") != "TASK-C03"
        or plan.get("selected_c03_path") != "CROSS_VENDOR"
        or plan.get("rounds") not in (2, 6)
        or plan.get("order_seed") != 3304
        or plan.get("topology") != expected_topology
        or plan.get("detector") != expected_detector
        or plan.get("arm_configs") != expected_arms
        or plan.get("arms") != ["BIG_ONLY", "ALL_CORES"]
    ):
        raise PreflightError(
            f"result directory contains incompatible existing experiment plan: {plan_path}"
        )


def perform_preflight(args):
    outdir = Path(args.outdir).resolve()
    preflight_dir = outdir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_current_preflight(preflight_dir)
    allowed = sorted(os.sched_getaffinity(0))
    identity = cpu_identity()
    records = collect_cpu_records(allowed=allowed)
    power_start = collect_power_state(records)
    cores = []
    selection = None
    build = None
    model = None
    status = "FAIL"
    warnings = []
    error = None
    try:
        cores = group_physical_cores(records)
        selection = classify_physical_cores(cores)
        validate_target(identity, records, cores, selection, args.allow_non_hx370)
        if args.allow_non_hx370:
            raise PreflightError(
                "--allow-non-hx370 is inspection-only and cannot produce a smoke-authorizing PASS"
            )
        model = validate_model_identity(args.model)
        build = verify_diagnostic_build(ROOT, args.server_bin)
        selected = {
            "selected_c03_path": "CROSS_VENDOR",
            "classification_source": selection["classification_source"],
            "classification_evidence": selection["classification_evidence"],
            "big_cpus": [core["representative_cpu"] for core in selection["big_cores"]],
            "compact_cpus": [core["representative_cpu"] for core in selection["compact_cores"]],
            "threads_big": 4,
            "threads_all": 12,
            "big_physical_cores": core_table(selection["big_cores"]),
            "compact_physical_cores": core_table(selection["compact_cores"]),
        }
        validate_masks(selected["big_cpus"], selected["compact_cpus"], cores)
        check_existing_plan(outdir, selected)
        status = "PASS"
    except AmbiguousClassification as exc:
        error = str(exc)
        cores = exc.cores or cores
        status = "PRECHECK_NEEDS_REVIEW"
    except (PreflightError, OSError, ValueError) as exc:
        error = str(exc)
        status = "FAIL"

    power = collect_power_state(records)
    thermal = collect_thermal_state()
    stable_power_fields = (
        "scaling_drivers", "governors", "epp", "amd_pstate_status",
        "amd_pstate_prefcore", "boost", "platform_profile",
    )
    power_changes = {
        field: {"start": power_start.get(field), "end": power.get(field)}
        for field in stable_power_fields
        if power_start.get(field) != power.get(field)
    }
    power["changes_during_preflight"] = power_changes
    if power_changes:
        power["warnings"].append(
            "power/profile state changed during preflight: "
            + json.dumps(power_changes, sort_keys=True)
        )
    warnings.extend(power["warnings"])
    warnings.extend(thermal["warnings"])
    git_meta = {
        "commit": git_output(["rev-parse", "HEAD"]),
        "branch": git_output(["branch", "--show-current"]),
        "dirty_porcelain": git_output(["status", "--porcelain", "--untracked-files=all"]),
        "tracked_dirty_porcelain": git_output(
            ["status", "--porcelain", "--untracked-files=no"]
        ),
        "submodule_status": git_output(["submodule", "status"]),
    }
    tracked_lines = [
        line.strip()
        for line in git_meta["tracked_dirty_porcelain"].splitlines()
        if line.strip() and "results/" not in line
    ]
    if status == "PASS" and tracked_lines not in (["m llama.cpp"], ["M llama.cpp"]):
        status = "FAIL"
        error = (
            "tracked repository state is not the clean AMD branch plus only "
            "the expected applied llama.cpp diagnostic patch: "
            f"{git_meta['tracked_dirty_porcelain'].splitlines()}"
        )
    if status == "PASS" and git_meta["branch"] != "AMD":
        status = "FAIL"
        error = f"C03 AMD bootstrap must run from branch AMD; observed {git_meta['branch']!r}"
    commands = {
        "uname -a": run_readonly(["uname", "-a"]),
        "lscpu": run_readonly(["lscpu"]),
        "lscpu -e": run_readonly(["lscpu", "-e"]),
        "proc_cpuinfo": run_readonly(["cat", "/proc/cpuinfo"]),
    }
    topology = {
        "schema_version": 1,
        "captured_at": utc_timestamp(),
        "cpu_identity": identity,
        "online_cpus": [record["cpu"] for record in records],
        "allowed_affinity": allowed,
        "logical_cpus": records,
        "physical_cores": core_table(cores),
    }
    status_record = {
        "schema_version": 1,
        "task": "TASK-C03",
        "precheck_status": status,
        "captured_at": utc_timestamp(),
        "error": error,
        "warnings": warnings,
        "archived_previous_preflight": archived,
        "git": git_meta,
        "cpu_identity": identity,
        "model": model,
        "diagnostic_build": build,
        "power": power,
        "thermal": thermal,
        "selected_topology_file": "selected_topology.json" if status == "PASS" else None,
        "debug_override_used": bool(args.allow_non_hx370),
    }
    atomic_json(preflight_dir / "topology.json", topology)
    atomic_write(
        preflight_dir / "environment.txt",
        environment_text(identity, records, allowed, commands, power, thermal, git_meta),
    )
    if status == "PASS":
        selected["model"] = model
        selected["diagnostic_build"] = build
        selected["git_commit"] = git_meta["commit"]
        selected["cpu_model"] = identity["model_name"]
        selected_path = preflight_dir / "selected_topology.json"
        env_path = preflight_dir / "c03_topology.env"
        atomic_json(selected_path, selected)
        atomic_write(
            env_path,
            topology_env_text(selected, build, model),
        )
        status_record["selected_topology_sha256"] = sha256_file(selected_path)
        status_record["topology_env_sha256"] = sha256_file(env_path)
    else:
        for stale in ("selected_topology.json", "c03_topology.env"):
            path = preflight_dir / stale
            if path.exists():
                path.unlink()
    summary_lines = [
        "# TASK-C03 AMD cross-vendor preflight",
        "",
        f"**PRECHECK STATUS: {status}**",
        "",
        f"- CPU model: {identity.get('model_name')}",
        f"- Vendor: {identity.get('vendor_id')}",
        f"- Physical/logical: {len(cores)}/{len(records)}",
        f"- Allowed affinity: {cpu_list_text(allowed)}",
        f"- Git commit: {git_meta.get('commit')}",
        f"- Error: {error or 'none'}",
        "",
        "## Physical-core evidence",
        "",
        *markdown_table(cores),
    ]
    if status == "PASS":
        summary_lines += [
            "", "## Frozen selection", "",
            f"- Classification source: {selected['classification_source']}",
            f"- BIG mask: {cpu_list_text(selected['big_cpus'])}",
            f"- COMPACT mask: {cpu_list_text(selected['compact_cpus'])}",
            "- Thread counts: BIG_ONLY=4, ALL_CORES=12",
            f"- Diagnostic binary SHA-256: {build['server_binary']['sha256']}",
            f"- Model SHA-256: {model['sha256']}",
        ]
    if warnings:
        summary_lines += ["", "## Warnings", "", *[f"- {warning}" for warning in warnings]]
    if status == "PRECHECK_NEEDS_REVIEW":
        summary_lines += [
            "", "Classification is ambiguous. Do not run the benchmark; send "
            "this table and the complete preflight directory for review.",
        ]
    atomic_write(preflight_dir / "preflight_summary.md", "\n".join(summary_lines) + "\n")
    atomic_json(preflight_dir / "preflight_status.json", status_record)
    print(f"PRECHECK STATUS: {status}")
    if status == "PASS":
        print(f"BIG MASK: {cpu_list_text(selected['big_cpus'])}")
        print(f"COMPACT MASK: {cpu_list_text(selected['compact_cpus'])}")
        print("THREAD COUNTS: BIG_ONLY=4 ALL_CORES=12")
    elif status == "PRECHECK_NEEDS_REVIEW":
        print("CPU classification is ambiguous; benchmark execution is forbidden.")
        print("\n".join(markdown_table(cores)))
    print(json.dumps({
        "precheck_status": status,
        "cpu_model": identity.get("model_name"),
        "error": error,
        "warnings": warnings,
        "preflight_directory": str(preflight_dir),
    }, indent=2))
    return 0 if status == "PASS" else (3 if status == "PRECHECK_NEEDS_REVIEW" else 2)


def verify_persisted_preflight(args):
    outdir = Path(args.outdir).resolve()
    preflight = outdir / "preflight"
    status = json.loads((preflight / "preflight_status.json").read_text(encoding="utf-8"))
    selected = json.loads((preflight / "selected_topology.json").read_text(encoding="utf-8"))
    if status.get("precheck_status") != "PASS" or status.get("debug_override_used"):
        raise PreflightError("smoke requires a non-override preflight PASS")
    selected_path = preflight / "selected_topology.json"
    env_path = preflight / "c03_topology.env"
    if sha256_file(selected_path) != status.get("selected_topology_sha256"):
        raise PreflightError("persisted selected_topology.json changed since preflight")
    if sha256_file(env_path) != status.get("topology_env_sha256"):
        raise PreflightError("persisted c03_topology.env changed since preflight")
    expected_env = topology_env_text(
        selected, selected["diagnostic_build"], selected["model"]
    )
    if env_path.read_text(encoding="utf-8") != expected_env:
        raise PreflightError("generated topology environment is not canonical")
    current_commit = git_output(["rev-parse", "HEAD"])
    if current_commit != selected.get("git_commit"):
        raise PreflightError(
            f"git commit changed since preflight: {selected.get('git_commit')} -> {current_commit}"
        )
    current_branch = git_output(["branch", "--show-current"])
    if current_branch != "AMD":
        raise PreflightError(f"smoke requires branch AMD; observed {current_branch!r}")
    current_tracked_dirty = [
        line.strip()
        for line in git_output(["status", "--porcelain", "--untracked-files=no"]).splitlines()
        if line.strip() and "results/" not in line
    ]
    status_tracked_dirty = [
        line.strip()
        for line in status.get("git", {}).get("tracked_dirty_porcelain", "").splitlines()
        if line.strip() and "results/" not in line
    ]
    if current_tracked_dirty != status_tracked_dirty:
        raise PreflightError("tracked repository dirty state changed since preflight")
    current_model = file_identity(selected["model"]["resolved_path"])
    if current_model["sha256"] != selected["model"]["sha256"]:
        raise PreflightError("model identity changed since preflight")
    build = verify_diagnostic_build(ROOT, selected["diagnostic_build"]["server_binary"]["resolved_path"])
    if build["server_binary"]["sha256"] != selected["diagnostic_build"]["server_binary"]["sha256"]:
        raise PreflightError("diagnostic binary changed since preflight")
    if (
        build["phase_mark_capability"]["matched_file_identities"]
        != selected["diagnostic_build"]["phase_mark_capability"]["matched_file_identities"]
    ):
        raise PreflightError("diagnostic PHASE_MARK shared-library identity changed since preflight")
    records = collect_cpu_records(allowed=sorted(os.sched_getaffinity(0)))
    cores = group_physical_cores(records)
    validate_masks(selected["big_cpus"], selected["compact_cpus"], cores)
    identity = cpu_identity()
    if identity.get("vendor_id") != "AuthenticAMD" or "Intel" in (identity.get("model_name") or ""):
        raise PreflightError("current machine is not the preflighted AMD target")
    current_power = collect_power_state(records)
    stable_power_fields = (
        "scaling_drivers", "governors", "epp", "amd_pstate_status",
        "amd_pstate_prefcore", "boost", "platform_profile",
    )
    changes = {
        field: {"preflight": status.get("power", {}).get(field), "current": current_power.get(field)}
        for field in stable_power_fields
        if status.get("power", {}).get(field) != current_power.get(field)
    }
    if changes:
        print(
            "PRECHECK WARNING: power/profile state changed since preflight: "
            + json.dumps(changes, sort_keys=True),
            file=sys.stderr,
        )
    selected_core_keys = {
        (core["package_id"], core["core_id"]): core for core in cores
    }
    for entry in selected["big_physical_cores"] + selected["compact_physical_cores"]:
        key = (entry["package_id"], entry["core_id"])
        current = selected_core_keys.get(key)
        if current is None or current["representative_cpu"] != entry["representative_cpu"]:
            raise PreflightError("persisted physical-core selection no longer matches live topology")
    check_existing_plan(outdir, selected)
    print(json.dumps({
        "verified": True,
        "big_cpus": selected["big_cpus"],
        "compact_cpus": selected["compact_cpus"],
        "threads_big": selected["threads_big"],
        "threads_all": selected["threads_all"],
    }))
    return 0


def handoff(args):
    outdir = Path(args.outdir).resolve()
    preflight = outdir / "preflight"
    status = json.loads((preflight / "preflight_status.json").read_text(encoding="utf-8"))
    selected = json.loads((preflight / "selected_topology.json").read_text(encoding="utf-8"))
    runs = []
    for path in sorted((outdir / "raw" / "runs").glob("round_*.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    valid = [run for run in runs if run.get("status") == "ok"]
    expected_runs = 12 if len(runs) > 4 else 4
    print(f"PRECHECK STATUS: {status.get('precheck_status')}")
    print(f"CPU MODEL: {selected.get('cpu_model')}")
    print(f"BIG MASK: {cpu_list_text(selected['big_cpus'])}")
    print(f"COMPACT MASK: {cpu_list_text(selected['compact_cpus'])}")
    print(f"THREAD COUNTS: BIG_ONLY={selected['threads_big']} ALL_CORES={selected['threads_all']}")
    print(f"GIT COMMIT: {selected.get('git_commit')}")
    print(f"BINARY ID: {selected['diagnostic_build']['server_binary']['sha256']}")
    print(f"MODEL ID: {selected['model']['sha256']}")
    print(f"RUN STATUS: {len(valid)}/{expected_runs} valid")
    print(f"OUTPUT DIRECTORY: {outdir}")
    print(f"SEND BACK: {outdir}")
    return 0 if len(valid) == expected_runs else 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "verify", "handoff"):
        item = sub.add_parser(name)
        item.add_argument(
            "--outdir", default=str(ROOT / "results" / "conference_c03")
        )
        if name == "preflight":
            item.add_argument(
                "--server-bin",
                default=str(ROOT / "llama.cpp" / "build-diag" / "bin" / "llama-server"),
            )
            item.add_argument(
                "--model", default=str(ROOT / "models" / EXPECTED_MODEL_NAME)
            )
            item.add_argument("--allow-non-hx370", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "preflight":
            return perform_preflight(args)
        if args.command == "verify":
            return verify_persisted_preflight(args)
        return handoff(args)
    except (PreflightError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"PRECHECK FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
