"""Read genuine upstream objects into an isolated archive; never change upstream.

The snapshot is historical Qbot, never installed as FactorLab or given a new
financial approval. The seven preimage attacks run their ORIGINAL test bodies.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "0ec7af5870c54a0ee202fbceeb97b7d87c293411"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--prepare-only", action="store_true")
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / "snapshot"
    env = {k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", MPLBACKEND="Agg", GIT_OPTIONAL_LOCKS="0")
    source = a.source.resolve()
    if source == ROOT or ROOT not in out.parents:
        raise ValueError("Archive must be inside the independent candidate, source must differ")
    def git(cwd, *args):
        return subprocess.check_output(["git", "-C", str(cwd), *args], env=env, stderr=subprocess.PIPE)
    git(source, "cat-file", "-e", SOURCE_COMMIT + "^{commit}")
    before = {"head": git(source, "rev-parse", "HEAD").decode().strip(),
              "status": git(source, "status", "--porcelain=v1").decode()}
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(source), str(snapshot)], check=True, env=env, capture_output=True)
    git(snapshot, "read-tree", SOURCE_COMMIT)
    # The original C01 registry pins platform-specific runtime bytes. Preserve
    # the real source checkout policy, never manufacture historical preimages.
    eol = subprocess.run(["git", "-C", str(source), "config", "--get", "core.autocrlf"],
                         env=env, capture_output=True, text=True).stdout.strip() or "false"
    if eol not in ("true", "false", "input"):
        raise ValueError("Unsupported original checkout EOL policy")
    git(snapshot, "config", "core.autocrlf", eol)
    git(snapshot, "checkout-index", "--all", "--force")
    entries = git(snapshot, "ls-files", "--stage").decode()
    gitlinks = [line for line in entries.splitlines() if line.startswith("160000 ")]
    bodies = {n: hashlib.sha256((snapshot/n).read_bytes()).hexdigest()
              for n in git(snapshot, "ls-files", "-z").decode().split("\0")
              if n and (snapshot/n).is_file()}
    receipt = {"schema": "QBOT_READONLY_ARCHIVE_REPLAY_V1", "source_commit": SOURCE_COMMIT,
               "source_before": before, "snapshot_raw_sha256": bodies, "git_entries": entries,
               "scope": "Original Git objects and original tests, no path synthesis or receipt edits",
               "approval": "BLOCKED", "financial_admission": False,
               "submodule_gitlinks_not_replayed": gitlinks, "source_checkout_autocrlf": eol}
    (out/"snapshot-identity.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print("Prepared genuine archive", snapshot, len(bodies), "files", flush=True)
    if a.prepare_only:
        return
    review = json.loads((ROOT/"docs/migration/evidence/full-failure-review.json").read_bytes())
    nodes = []
    for group in review["distinct_message_groups"]:
        for case in group["cases"]:
            module = group["module"] or case["name"]
            node = module.replace(".", "/") + ".py"
            if group["module"]:
                node += "::" + case["name"]
            nodes.append(node)
    command = [sys.executable, "-m", "pytest", *dict.fromkeys(nodes), "-q", "-ra",
               "--continue-on-collection-errors", "-p", "no:cacheprovider",
               "--basetemp="+str(out/"tmp"), "--junitxml="+str(out/"tests.xml")]
    env["PYTHONPATH"] = str(snapshot)
    with (out/"tests.log").open("w", encoding="utf-8") as log:
        code = subprocess.call(command, cwd=snapshot, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = {"head": git(source, "rev-parse", "HEAD").decode().strip(),
             "status": git(source, "status", "--porcelain=v1").decode()}
    changed = [n for n,d in bodies.items() if not (snapshot/n).is_file() or hashlib.sha256((snapshot/n).read_bytes()).hexdigest()!=d]
    receipt.update(exit_code=code, source_after=after, source_unchanged=before==after,
                   snapshot_changed_files=changed, status="PASS" if code==0 and before==after and not changed else "FAIL")
    if (out/"tests.xml").is_file():
        receipt["junit_suites"] = [s.attrib for s in ET.parse(out/"tests.xml").getroot().iter("testsuite")]
    (out/"receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(receipt["status"], "archive replay", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
