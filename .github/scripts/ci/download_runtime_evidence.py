"""Download immutable Actions evidence via authenticated gh, without changing CI."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import zipfile

from benchmark import write_json


REPOSITORY = "Wind-Maker1001/quant-tv"


def gh(*arguments):
    return subprocess.check_output(["gh", *arguments])


def save_immutable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise ValueError("Existing audit evidence differs: " + str(path))
    if not path.exists():
        path.write_bytes(data)


def download(run_id, output):
    prefix = "repos/" + REPOSITORY
    run = json.loads(gh("api", prefix + "/actions/runs/" + str(run_id)))
    if run["status"] != "completed":
        print(json.dumps({"run_id": run_id, "status": run["status"]}))
        return
    destination = output / str(run_id)
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "download-receipt.json").exists():
        print(json.dumps({"run_id": run_id, "evidence": "already downloaded"}))
        return
    write_json(destination / "run.json", run)
    jobs = json.loads(
        gh("api", prefix + "/actions/runs/" + str(run_id) + "/jobs?per_page=100")
    )
    write_json(destination / "jobs.json", jobs)
    save_immutable(
        destination / "run.log",
        gh("run", "view", str(run_id), "--repo", REPOSITORY, "--log"),
    )
    artifacts = json.loads(
        gh("api", prefix + "/actions/runs/" + str(run_id) + "/artifacts?per_page=100")
    )
    write_json(destination / "artifacts.json", artifacts)
    receipt = {
        "run_id": run_id,
        "url": run["html_url"],
        "run_head_sha": run["head_sha"],
        "checkout_sha_source": "artifact environment.json; PR checkout may be a merge commit",
        "event": run["event"],
        "conclusion": run["conclusion"],
        "artifacts": [],
    }
    for artifact in artifacts["artifacts"]:
        if not re.fullmatch(
            r"(?:ci-runtime|research-evidence)-[\w.-]+", artifact["name"]
        ):
            continue
        if artifact["expired"]:
            raise ValueError("Required artifact expired")
        archive = gh(
            "api", prefix + "/actions/artifacts/" + str(artifact["id"]) + "/zip"
        )
        target = (destination / artifact["name"]).resolve()
        files = {}
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for name in zipped.namelist():
                path = (target / name).resolve()
                if not path.is_relative_to(target):
                    raise ValueError("Artifact path escapes audit directory")
                if name.endswith("/"):
                    continue
                data = zipped.read(name)
                save_immutable(path, data)
                files[name] = hashlib.sha256(data).hexdigest()
        receipt["artifacts"].append(
            {
                "id": artifact["id"],
                "name": artifact["name"],
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "files_sha256": files,
            }
        )
    write_json(destination / "download-receipt.json", receipt)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "conclusion": run["conclusion"],
                "artifact_count": len(receipt["artifacts"]),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", type=int, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for identifier in args.run_ids:
        download(identifier, args.output)
