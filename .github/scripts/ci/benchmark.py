"""Run the complete offline suite and retain timings, exit status and evidence."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time


def canonical_ids(values):
    return ("\n".join(sorted(values)) + "\n").encode("utf-8")


def write_json(path, value):
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def process_tree_rss(pid):
    """Sample aggregate Linux process-tree RSS; shared pages may be counted twice."""
    processes = {}
    try:
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    fields = {}
                    for line in (entry / "status").read_text().splitlines():
                        if line.startswith(("PPid:", "VmRSS:")):
                            key, value = line.split(":", 1)
                            fields[key] = int(value.split()[0])
                    processes[int(entry.name)] = (
                        fields.get("PPid", 0),
                        fields.get("VmRSS", 0) * 1024,
                    )
                except (OSError, ValueError):
                    continue
    except OSError:
        return None
    selected = {pid}
    while True:
        expanded = selected | {
            key for key, (parent, _) in processes.items() if parent in selected
        }
        if expanded == selected:
            break
        selected = expanded
    return sum(processes.get(key, (0, 0))[1] for key in selected)


def usage():
    try:
        import resource

        result = resource.getrusage(resource.RUSAGE_CHILDREN)
        return result.ru_utime, result.ru_stime
    except ImportError:
        return None


def cpu_environment():
    try:
        models = sorted(
            {
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            }
        )
        quota = Path("/sys/fs/cgroup/cpu.max")
        return {
            "model_names": models,
            "cgroup_cpu_max": (
                quota.read_text().strip() if quota.exists() else "UNKNOWN"
            ),
        }
    except OSError:
        return "UNKNOWN"


def memory_headroom():
    """Observe available machine memory and a numeric cgroup v2 limit if present."""
    try:
        fields = {
            line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        }
        total, available = fields["MemTotal"], fields["MemAvailable"]
        fraction = available / total
        limit_path = Path("/sys/fs/cgroup/memory.max")
        limit = limit_path.read_text().strip() if limit_path.exists() else "UNKNOWN"
        if limit.isdigit():
            current = int(Path("/sys/fs/cgroup/memory.current").read_text())
            fraction = min(fraction, (int(limit) - current) / int(limit))
        return total, available, fraction
    except (OSError, ValueError, KeyError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(0, 2, 3), default=0)
    parser.add_argument("--no-coverage", action="store_true")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-hotspots", action="store_true")
    parser.add_argument("--base-temp", type=Path)
    args = parser.parse_args()
    if args.profile_hotspots and (
        not args.profile or args.workers or args.collect_only
    ):
        parser.error(
            "Hotspot profiling requires serial --profile and is not acceptance"
        )
    if args.profile_hotspots:
        args.no_coverage = True
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    worker_state = output.parent / (output.name + "-worker-state")
    worker_state.mkdir(exist_ok=False)
    scripts = Path(__file__).resolve().parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(scripts) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    environment["MPLBACKEND"] = "Agg"
    environment["PYTHONUTF8"] = "1"
    environment["CI_WORKER_STATE_ROOT"] = str(worker_state)
    environment["CI_WORKER_EVIDENCE_DIR"] = str(output)
    command = [sys.executable]
    if args.profile:
        command.extend(
            ["-m", "cProfile", "-o", str(output / "profile.pstats"), "-m", "pytest"]
        )
    else:
        command.extend(["-m", "pytest"])
    command.extend(
        [
            *(
                [
                    "tests/research/test_cli_phase2.py",
                    "tests/research/test_five_year_pilot.py",
                    "tests/research/test_models_phase2.py",
                    "tests/research/test_tree_models_phase3.py",
                    "tests/research/test_phase2_synthetic_example.py",
                ]
                if args.profile_hotspots
                else ["tests/research"]
            ),
            "-q",
            "-m",
            "not network",
            "--durations=40",
            "--durations-min=0",
            "--rootdir=.",
            "-c",
            "pytest.ini",
            "-p",
            "ci_resources",
            "-p",
            "ci_profile",
            "--ci-evidence-dir=" + str(output),
        ]
    )
    if args.base_temp is not None:
        base_temp = args.base_temp.resolve()
        if base_temp.exists():
            raise ValueError("A fresh, nonexistent base-temp is required")
        command.append("--basetemp=" + str(base_temp))
    if args.collect_only:
        command.append("--collect-only")
    else:
        command.append("--junitxml=" + str(output / "research.xml"))
        if not args.no_coverage:
            command.extend(
                [
                    "--cov=qbot.research",
                    "--cov-report=term",
                    "--cov-report=xml:" + str(output / "coverage.xml"),
                ]
            )
        if args.workers:
            command.extend(
                ["-n", str(args.workers), "--dist=loadfile", "--max-worker-restart=0"]
            )
    package_versions = json.loads(
        subprocess.check_output(
            [sys.executable, "-m", "pip", "list", "--format=json"], text=True
        )
    )
    metadata = {
        "kind": "actual_ci_measurement_not_financial_certification",
        "subject_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "subject_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], text=True
        ).strip(),
        "subject_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        ),
        "subject_root": str(Path.cwd().resolve()),
        "worker_state_root": str(worker_state),
        "worker_resources_plugin_sha256": hashlib.sha256(
            (scripts / "ci_resources.py").read_bytes()
        ).hexdigest(),
        "pytest_ini_sha256": hashlib.sha256(
            Path("pytest.ini").read_bytes()
        ).hexdigest(),
        "logical_cpu_count": os.cpu_count(),
        "cpu_environment": cpu_environment(),
        "processor": platform.processor() or "UNKNOWN",
        "controller_sha": os.environ.get("CI_CONTROLLER_SHA", "UNKNOWN"),
        "python": sys.version,
        "platform": platform.platform(),
        "packages": package_versions,
        "command": command,
        "utc_started": datetime.now(timezone.utc).isoformat(),
        "workers": args.workers,
        "coverage": not args.no_coverage and not args.collect_only,
        "collect_only": args.collect_only,
        "profiled": args.profile,
        "execution_scope": (
            "hotspot_profile_not_full_acceptance"
            if args.profile_hotspots
            else "full_offline_suite"
        ),
        "environment": {
            key: environment.get(key, "UNSET")
            for key in (
                "ImageOS",
                "ImageVersion",
                "RUNNER_ARCH",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "CI_PIP_CACHE",
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
        "observer_sha256": hashlib.sha256(
            (scripts / "ci_profile.py").read_bytes()
        ).hexdigest(),
    }
    write_json(output / "environment.json", metadata)
    before = usage()
    started = time.perf_counter()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment
    )
    peak = [None]
    memory = {
        "physical_total_bytes": "UNKNOWN",
        "minimum_available_bytes": "UNKNOWN",
        "minimum_headroom_fraction": "UNKNOWN",
    }
    stopped = threading.Event()

    def sample():
        while not stopped.is_set():
            current = process_tree_rss(process.pid)
            if current is not None:
                peak[0] = max(peak[0] or 0, current)
            headroom = memory_headroom()
            if headroom is not None:
                memory["physical_total_bytes"] = headroom[0]
                previous = memory["minimum_available_bytes"]
                memory["minimum_available_bytes"] = (
                    min(previous, headroom[1])
                    if isinstance(previous, int)
                    else headroom[1]
                )
                previous_fraction = memory["minimum_headroom_fraction"]
                memory["minimum_headroom_fraction"] = (
                    min(previous_fraction, headroom[2])
                    if isinstance(previous_fraction, (int, float))
                    else headroom[2]
                )
            stopped.wait(0.25)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        with (output / "pytest.log").open("wb") as logfile:
            for line in iter(process.stdout.readline, b""):
                logfile.write(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        exit_code = process.wait()
    finally:
        stopped.set()
        sampler.join()
    elapsed = time.perf_counter() - started
    after = usage()
    result = {
        "exit_code": exit_code,
        "wall_seconds": elapsed,
        "cpu_user_seconds": after[0] - before[0] if before and after else "UNKNOWN",
        "cpu_system_seconds": after[1] - before[1] if before and after else "UNKNOWN",
        "sampled_process_tree_rss_peak_bytes": (
            peak[0] if peak[0] is not None else "UNKNOWN"
        ),
        "rss_sampling_seconds": 0.25,
        "rss_shared_pages_may_be_double_counted": True,
        "sampled_memory_headroom": memory,
    }
    write_json(output / "result.json", result)
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(output.glob("pytest-*.json"))
    ]
    collector = next((row for row in observations if row["all_ids"]), None)
    if collector:
        for key in ("all_ids", "selected_ids", "deselected_ids"):
            data = canonical_ids(collector[key])
            (output / (key + ".txt")).write_bytes(data)
            result[key + "_count"] = len(collector[key])
            result[key + "_sha256"] = hashlib.sha256(data).hexdigest()
        write_json(output / "result.json", result)
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
