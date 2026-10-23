"""Summarize actual phase timings; profiling runs are not speed comparisons."""

import argparse
import csv
import json
from pathlib import Path
import pstats

from benchmark import write_json


def short_path(value):
    value = value.replace("\\", "/")
    for marker in ("/qbot/", "/tests/", "/examples/", "/site-packages/"):
        if marker in value:
            return (
                value.split(marker, 1)[1]
                if marker == "/site-packages/"
                else marker[1:] + value.split(marker, 1)[1]
            )
    return Path(value).name


def summarize(directory):
    directory = Path(directory)
    result = json.loads((directory / "result.json").read_text())
    environment = json.loads((directory / "environment.json").read_text())
    controller = json.loads((directory / "pytest-controller.json").read_text())
    reports = controller["reports"]
    slowest = sorted(reports, key=lambda row: row["seconds"], reverse=True)[:40]
    with (directory / "phase-top40.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(
            stream,
            lineterminator="\n",
            fieldnames=(
                "rank",
                "file",
                "nodeid",
                "phase",
                "seconds",
                "worker",
                "outcome",
            ),
        )
        writer.writeheader()
        for rank, row in enumerate(slowest, 1):
            writer.writerow(
                {
                    "rank": rank,
                    "file": row["nodeid"].split("::", 1)[0],
                    "nodeid": row["nodeid"],
                    "phase": row["when"],
                    "seconds": row["seconds"],
                    "worker": row["worker"],
                    "outcome": row["outcome"],
                }
            )
    collectors = [
        json.loads(path.read_text()) for path in directory.glob("pytest-*.json")
    ]
    summary = {
        "kind": "actual_software_ci_timing_only",
        "subject_sha": environment["subject_sha"],
        "controller_sha": environment["controller_sha"],
        "profiled": environment["profiled"],
        "workers": environment["workers"],
        "coverage": environment["coverage"],
        "result": result,
        "phase_seconds_sum_not_parallel_wall_time": {
            phase: sum(row["seconds"] for row in reports if row["when"] == phase)
            for phase in ("setup", "call", "teardown")
        },
        "collection_seconds_by_process": {
            row["worker"]: row["collection_seconds"] for row in collectors
        },
        "sessionfinish_seconds_by_process": {
            row["worker"]: row.get(
                "report_generation_and_sessionfinish_seconds", "UNKNOWN"
            )
            for row in collectors
        },
        "slowest_40": slowest,
    }
    profile = directory / "profile.pstats"
    if profile.exists():
        stats = pstats.Stats(str(profile))
        entries = []
        for (filename, line, function), (
            primitive,
            calls,
            own,
            cumulative,
            _,
        ) in stats.stats.items():
            entries.append(
                {
                    "file": short_path(filename),
                    "line": line,
                    "function": function,
                    "calls": calls,
                    "primitive_calls": primitive,
                    "self_seconds": own,
                    "inclusive_seconds_do_not_sum": cumulative,
                }
            )
        entries.sort(key=lambda row: row["inclusive_seconds_do_not_sum"], reverse=True)
        write_json(directory / "profile-top-functions.json", entries[:150])
        interfaces = [
            row
            for row in entries
            if row["file"].startswith("qbot/research/models/")
            and row["function"] in ("fit", "predict", "save", "load")
        ]
        write_json(directory / "profile-model-interfaces.json", interfaces)
        summary["profile_total_seconds"] = stats.total_tt
        summary["profile_model_interfaces"] = interfaces
    write_json(directory / "summary.json", summary)
    print(
        json.dumps(
            {
                key: value
                for key, value in summary.items()
                if key not in ("slowest_40", "profile_model_interfaces")
            },
            sort_keys=True,
        )
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    summarize(parser.parse_args().directory)
