"""Resource admission and evidence-based placement; never allocates cloud runtimes."""

from __future__ import annotations

import csv
import ctypes
import math
import os
from pathlib import Path
import subprocess

_MIB = 1024**2
_REPORT = "docs/validation/mc-colab-02/REPORT.md"


def _host_available_bytes() -> int:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total_phys", "avail_phys", "total_page", "avail_page",
                    "total_virtual", "avail_virtual", "avail_extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError()
        return int(status.avail_phys)
    if Path("/proc/meminfo").is_file():
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("Available host RAM requires Windows, Linux /proc, or psutil") from exc
    return int(psutil.virtual_memory().available)


def probe_resources(include_gpu: bool = True) -> dict:
    """Return host_available_bytes and gpus (index/name/free/total bytes/utilization).

    GPU discovery uses only nvidia-smi, not a CUDA runtime import. Missing or
    inaccessible NVIDIA tooling is reported in gpu_probe_error. Pass False for
    CPU-only execution; this performs no GPU subprocess or discovery.
    """
    result = {"host_available_bytes": _host_available_bytes(), "gpus": []}
    if not include_gpu:
        return result
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        for row in csv.reader(completed.stdout.splitlines()):
            index, name, total, free, utilization = (value.strip() for value in row)
            result["gpus"].append({
                "index": int(index), "name": name,
                "total_memory_bytes": int(total) * _MIB,
                "free_memory_bytes": int(free) * _MIB,
                "utilization_percent": float(utilization),
            })
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["gpus"] = []
        result["gpu_probe_error"] = str(exc)
    return result


def _positive(value, name, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _memory(task):
    p = task["parameters"]
    if task['entry'] == 'benchmark' and 'cells' in p:
        workers = _positive(p['workers'], 'workers', integer=True)
        from factorlab.experiments.research_workloads import benchmark_memory
        device = 'cpu' if task['device'] in ('cpu', 'auto') else 'gpu'
        estimates = [benchmark_memory(cell, workers, device,
            free_gpu_bytes=task.get('_free_gpu_bytes'),
            gpu_limit_mb=task.get('gpu_memory_limit_mb')) for cell in p['cells']]
        budget = task['memory_budget_mb'] * _MIB
        host = max(e['estimated_disk_working_bytes']
            if not p['statistics'] and e['estimated_in_memory_bytes'] > budget
            else e['estimated_in_memory_bytes'] for e in estimates)
        gpu = max(e['estimated_gpu_bytes'] for e in estimates)
        return host, gpu, dict(cells=estimates, workers=workers,
            memory_basis='sequential cells; concurrent seeds of one cell only')
    paths = _positive(p["paths"], "paths", integer=True)
    steps = _positive(p["steps"], "steps", integer=True)
    assets = _positive(p["assets"], "assets", integer=True)
    batch_value = p.get("batch_size")
    batch = min(paths, _positive(4096 if batch_value is None else batch_value, "batch_size", integer=True))
    history = _positive(p.get("history_rows", steps), "history_rows", integer=True)
    # Mirrors the runner's conservative per-path planner, with model/history
    # matrices, exact-summary copies, and separate process/CUDA overhead reserves.
    model = 8 * (assets * assets * 4 + history * assets * 4 + steps * assets * 2) + 65536
    workspace = batch * (8 * ((8 * assets + 16) * steps + 128) + 4096)
    vectors = paths * 8 * 8
    full_paths = paths * (steps + 1) * assets * 8 if p.get("save_full_paths", False) else 0
    if task["entry"] == "static_grid" or (task["entry"] == "benchmark" and p.get("model") == "static_grid"):
        # The registered grid adapter streams generated quotes and memmaps terminals.
        full_paths = max(full_paths, batch * (steps + 1) * assets * 8)
        vectors = paths * 8 * 20  # Sixteen terminal fields plus exact risk-summary workspace.
    workers = _positive(p.get('workers', 1), 'workers', integer=True)
    host = workers * (128 * _MIB + model + workspace + vectors + full_paths)
    gpu = 256 * _MIB + model + workspace
    return host, gpu, {"batch_size": batch, "model_bytes": model,
                       "workspace_bytes": workspace, "result_bytes": vectors,
                       "full_path_bytes": full_paths,
                       "memory_basis": "float64, bounded batch planner, 8 result/summary vectors, host/CUDA reserves"}


def _report_evidence(task, resources):
    p = task["parameters"]
    if task['entry'] == 'benchmark' and 'cells' in p:
        return None
    model = task["entry"] if task["entry"] != "benchmark" else p.get("model")
    matched = (model in ("gbm", "bootstrap") and p["assets"] == 1
               and p["steps"] == 252 and not p.get("save_full_paths", False)
               and set(p) <= {"paths", "steps", "assets", "batch_size", "seed", "model", "history_rows"}
               and p.get('history_rows', 4096) == 4096
               and not task.get("inputs"))
    warm = resources.get("gpu_warm", False)
    batch = min(p["paths"], p.get("batch_size") or 4096)
    if matched and p["paths"] == 100000 and batch == 1024:
        cpu, gpu = ((1.003568, .094320) if model == "gbm" else (.782215, .109847)) if warm else (
            (2.467, 3.596) if model == "gbm" else (2.297, 3.156))
        return {"source": _REPORT, "scope": "steady" if warm else "cold_process",
                "cpu_seconds": cpu, "local_gpu_seconds": gpu,
                "reliability": "five seeds", "hardware": "i7-14700KF / RTX 2070"}
    if matched and p["paths"] == 1000000 and batch == 4096 and warm:
        cpu, gpu = (17.041418, .455560) if model == "gbm" else (14.445657, .288307)
        return {"source": _REPORT, "scope": "steady", "cpu_seconds": cpu,
                "local_gpu_seconds": gpu, "reliability": "one seed, two warm repeats",
                "hardware": "i7-14700KF / RTX 2070"}
    return None


def select_task(task: dict, resources: dict | None, cloud: dict | None = None) -> dict:
    """Choose cpu/local_gpu/colab, or raise ValueError/RuntimeError on refusal.

    resources=None probes locally (CPU requests disable GPU discovery). Resource
    dictionaries use probe_resources' schema, plus optional gpu_warm. Cloud must
    supply authorized, available, balance_cu, budget_cu, rate_per_hour,
    startup_seconds, host_memory_bytes, gpu_memory_bytes. CU admission reserves
    startup plus the entire work timeout, not an optimistic kernel estimate.
    throughput_opt_in on the task permits auto cloud placement. Optional
    measurements on resources/cloud are report-backed records with entry,
    parameters (exact match), source, reliable=True, scope='full_lifecycle', and
    seconds; these alone can establish an empirical cloud lifecycle advantage.
    estimated_seconds/estimated_cost_cu are None when not supported by evidence.
    """
    choice = task["device"]
    if choice not in ("cpu", "local_gpu", "colab", "auto"):
        raise ValueError("device must be cpu, local_gpu, colab or auto")
    if task["entry"] not in ("gbm", "bootstrap", "static_grid", "benchmark"):
        raise ValueError("unsupported task entry")
    budget = _positive(task["memory_budget_mb"], "memory_budget_mb", integer=True) * _MIB
    timeout = _positive(task["timeout_seconds"], "timeout_seconds")
    limit = task.get("gpu_memory_limit_mb")
    if limit is not None:
        limit = _positive(limit, "gpu_memory_limit_mb", integer=True) * _MIB
    host, gpu, memory = _memory(task)
    resources = probe_resources(include_gpu=choice != "cpu") if resources is None else resources
    local_host = host <= min(budget, resources["host_available_bytes"])
    gpu_workers_ok = task['entry'] != 'benchmark' or task['parameters'].get('requested_workers', 1) == 1
    eligible = []
    for candidate in resources.get('gpus', []) if gpu_workers_ok else []:
        if task['entry'] == 'benchmark' and 'cells' in task['parameters']:
            try:
                candidate_host, candidate_gpu, _ = _memory(dict(task, device='local_gpu',
                    _free_gpu_bytes=candidate['free_memory_bytes']))
            except RuntimeError:
                continue
            fits = candidate_host <= min(budget, resources['host_available_bytes']) and candidate_gpu <= candidate['free_memory_bytes']
        else:
            fits = local_host and gpu <= min(candidate['free_memory_bytes'], limit if limit is not None else math.inf)
        if fits:
            eligible.append(candidate)
    idle = [g for g in eligible if g["utilization_percent"] < 20]
    evidence = [memory]
    measured = _report_evidence(task, resources)
    if measured:
        evidence.append(measured)

    def lifecycle(metadata):
        return [r for r in metadata.get("measurements", [])
                if r.get("entry") == task["entry"] and r.get("parameters") == task["parameters"]
                and r.get("scope") == "full_lifecycle" and r.get("reliable") is True
                and r.get("source") and isinstance(r.get("seconds"), (int, float))
                and math.isfinite(r["seconds"]) and r["seconds"] > 0]

    local_times = [r for r in lifecycle(resources) if r.get("device") in ("cpu", "local_gpu")]
    cloud_times = [r for r in lifecycle(cloud or {}) if r.get("device") == "colab"]
    advantage = bool(local_times and cloud_times and
                     max(r["seconds"] for r in cloud_times) < min(r["seconds"] for r in local_times))
    cloud_error = "cloud authorization and confirmed availability required"
    cloud_ok = False
    reserved_cost = None
    if cloud and cloud.get("authorized") is True and cloud.get("available") is True:
        required = ("balance_cu", "budget_cu", "rate_per_hour", "startup_seconds",
                    "host_memory_bytes", "gpu_memory_bytes")
        if all(k in cloud for k in required):
            for key in required:
                value = cloud[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"cloud.{key} must be finite and nonnegative")
            reserved_cost = (cloud["startup_seconds"] + timeout) * cloud["rate_per_hour"] / 3600
            cloud_host, cloud_gpu = host, gpu
            cloud_gpu_limit = limit if limit is not None else math.inf
            if task['entry'] == 'benchmark' and 'cells' in task['parameters']:
                try:
                    cloud_host, cloud_gpu, _ = _memory(dict(task, device='colab',
                        _free_gpu_bytes=cloud['gpu_memory_bytes']))
                except RuntimeError:
                    cloud_gpu = math.inf
                cloud_gpu_limit = math.inf  # Pool caps are applied per cell, excluding CUDA context.
            cloud_ok = (gpu_workers_ok and cloud["rate_per_hour"] > 0
                        and reserved_cost <= min(cloud["balance_cu"], cloud["budget_cu"])
                        and cloud_host <= min(budget, cloud["host_memory_bytes"])
                        and cloud_gpu <= min(cloud["gpu_memory_bytes"], cloud_gpu_limit))
            cloud_error = "cloud memory, positive active rate, balance or authorized CU budget insufficient"
        else:
            cloud_error = "cloud capacity, startup, rate, balance and budget metadata required"

    selected_gpu = None
    if choice == "cpu":
        if not local_host:
            raise RuntimeError("explicit CPU placement: insufficient available host RAM or memory budget")
        device, reason = "cpu", "explicit CPU placement; no fallback"
    elif choice == "local_gpu":
        if not eligible:
            raise RuntimeError("explicit local GPU placement: GPU or host memory unavailable")
        selected_gpu = eligible[0]
        device, reason = "local_gpu", "explicit local GPU placement; no fallback"
    elif choice == "colab":
        if not cloud_ok:
            raise RuntimeError(f"explicit Colab placement: {cloud_error}")
        device, reason = "colab", "explicit authorized Colab placement; no fallback"
    elif cloud_ok and (not local_host or (resources.get("gpus") and not idle)
                       or task.get("throughput_opt_in") is True or advantage):
        device = "colab"
        reason = ("reliable matched full-lifecycle reports favor cloud" if advantage else
                  "explicit cloud throughput opt-in" if task.get("throughput_opt_in") is True else
                  "local GPU unavailable, busy (>=20%), or insufficient memory")
    elif idle and measured and measured["local_gpu_seconds"] < measured["cpu_seconds"]:
        selected_gpu = idle[0]
        device, reason = "local_gpu", "matched workload report favors steady local GPU; not a generic speed model"
    elif local_host:
        device = "cpu"
        reason = ("matched cold-process report favors CPU for one-off workload" if measured and measured["scope"] == "cold_process"
                  else "conservative CPU placement; no matched lifecycle evidence establishes an accelerator advantage")
    else:
        raise RuntimeError("no authorized placement fits available host/GPU memory and budgets")

    if selected_gpu is not None and task['entry'] == 'benchmark' and 'cells' in task['parameters']:
        host, gpu, memory = _memory(dict(task, device='local_gpu',
            _free_gpu_bytes=selected_gpu['free_memory_bytes']))
        evidence[0] = memory
    if device == 'colab' and task['entry'] == 'benchmark' and 'cells' in task['parameters']:
        host, gpu, memory = _memory(dict(task, device='colab',
            _free_gpu_bytes=cloud['gpu_memory_bytes']))
        evidence[0] = memory
    seconds = None
    cost = None
    if device == "colab":
        evidence.append({"reserved_cost_cu": reserved_cost, "basis": "startup_seconds + timeout_seconds"})
        if cloud_times:
            evidence.extend(cloud_times)
            seconds = max(r["seconds"] for r in cloud_times)
            cost = seconds * cloud["rate_per_hour"] / 3600
    elif local_times:
        # Device-specific lifecycle evidence is required for a placement estimate.
        matching = [r for r in local_times if r.get("device") == device]
        if matching:
            evidence.extend(matching)
            seconds = max(r["seconds"] for r in matching)
    result = {"device": device, "compute_device": "cpu" if device == "cpu" else "gpu",
              "reason": reason, "estimated_seconds": seconds, "estimated_cost_cu": cost,
              "estimated_host_bytes": host, "estimated_gpu_bytes": 0 if device == "cpu" else gpu,
              "evidence": evidence}
    if selected_gpu is not None:
        result["gpu_index"] = selected_gpu["index"]
    return result
