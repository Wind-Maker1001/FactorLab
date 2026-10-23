"""Real squash transfer and pinned-manifest refusals. NO LIVE MONEY."""

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from tests.research.test_c01_preservation import (  # noqa: F401
    ROOT,
    candidate,
    control,
    controller_at,
)


def command(root, *args, env=None, check=True):
    clean = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "COV_CORE_", "COVERAGE_"))
        and key not in ("PYTHONPATH", "PYTHONHOME")
    }
    clean.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", GIT_LFS_SKIP_SMUDGE="1")
    if env:
        clean.update(env)
    result = subprocess.run(
        args,
        cwd=root,
        env=clean,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(result.stdout.decode("utf-8", errors="replace")[-16000:])
    return result


def test_squash_fresh_clone_without_source_objects_preserves_and_refuses_finance(
    tmp_path, control, request
):
    # During local development the reviewed staged tree can precede a commit.
    # In final-HEAD CI it is exactly the clean committed candidate tree.
    tree = command(ROOT, "git", "write-tree").stdout.decode().strip()
    identity = {
        "GIT_AUTHOR_NAME": "C01 synthetic isolation",
        "GIT_AUTHOR_EMAIL": "c01@example.invalid",
        "GIT_COMMITTER_NAME": "C01 synthetic isolation",
        "GIT_COMMITTER_EMAIL": "c01@example.invalid",
    }
    squash = (
        command(
            ROOT,
            "git",
            "commit-tree",
            tree,
            "-p",
            control.LEGACY,
            "-m",
            "Synthetic squash main; no development history",
            env=identity,
        )
        .stdout.decode()
        .strip()
    )
    origin = tmp_path / "origin.git"
    origin.mkdir()
    command(origin, "git", "init", "--bare")
    command(
        origin,
        "git",
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        str(ROOT),
        squash + ":refs/heads/main",
    )
    assert not (origin / "objects/info/alternates").exists()
    for oid in (
        control.SOURCE,
        control.REVIEWED_SOURCE,
        "f7a4490f5ecd6c64fda96794d343b51e1caa307c",
    ):
        assert (
            command(
                origin, "git", "cat-file", "-e", oid + "^{commit}", check=False
            ).returncode
            != 0
        )
    root = tmp_path / "fresh-clone"
    rendering = "true" if os.name == "nt" else "false"
    command(
        tmp_path,
        "git",
        "-c",
        "core.autocrlf=" + rendering,
        "clone",
        "--no-local",
        "--no-hardlinks",
        "--branch",
        "main",
        str(origin),
        str(root),
    )
    assert not (root / ".git/objects/info/alternates").exists()
    refs = (
        command(root, "git", "for-each-ref", "--format=%(refname)")
        .stdout.decode()
        .splitlines()
    )
    assert refs and all(
        ref in {"refs/heads/main", "refs/remotes/origin/main"} for ref in refs
    )
    absent = command(
        root, "git", "cat-file", "-e", control.SOURCE + "^{commit}", check=False
    )
    assert absent.returncode != 0
    assert command(root, "git", "rev-parse", "HEAD").stdout.decode().strip() == squash
    worker = r"""
from dataclasses import replace
import importlib.util,json
from pathlib import Path
from types import SimpleNamespace
r=Path.cwd()
s=importlib.util.spec_from_file_location('isolated_controller',r/'.github/scripts/ci/research_checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
c=m.load_security_revision(r);v=c.validate_preservation(r);c.load_revision(r)
assert len(v['source_closure'])==35 and not v['financial_recertification']
from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.evidence import sha256
from factorlab.data.market_qualification import load_qualification,publish_qualification
from factorlab.data.providers.base import MarketQualificationEvidence
from factorlab.errors import ProviderError
from tests.research.market_data_fixture import qualification_inputs
import factorlab,tests.research.market_data_fixture
assert Path(factorlab.__file__).resolve().is_relative_to(r)
assert Path(tests.research.market_data_fixture.__file__).resolve().is_relative_to(r)
from examples.research.review_yhh30m_release import review
publication=review(r,c.LEGACY)
assert publication['status']=='PASS',publication['blocked_findings']
work=r.parent/'synthetic-finance';work.mkdir()
inputs,keys,dates,stocks,benchmark=qualification_inputs(work)
inputs=replace(inputs,membership=inputs.membership.iloc[:-1])
path=publish_qualification(inputs,keys,dates,stocks=stocks,benchmark=benchmark,output_root=work/'package')
report,*_=load_qualification(path,stocks=stocks,benchmark=benchmark)
assert report.stages['PIT']=='INCOMPLETE' and not report.formal_admission and not report.execution_admission
evidence=MarketQualificationEvidence(path,sha256(path.read_bytes()),report.source_version,stocks,benchmark)
provider=SimpleNamespace(get_research_qualification=lambda:evidence)
try:
 assert_formal_market_admission(provider,membership=inputs.membership,calendar=dates,bars=stocks,
   benchmark=benchmark,research_start=inputs.research_start,research_end=inputs.research_end,
   status=inputs.status,listings=inputs.listings)
except ProviderError:
 pass
else:
 raise AssertionError('Incomplete PIT gained formal admission')
print(json.dumps({'status':'C01_SOURCE_UNREACHABLE_REPLAY_PASS','source_object_required':False,
 'closure':35,'formal_admission':False,'execution_admission':False,
 'publication_files':len(publication['safety_checked_files']),'publication_findings':0}))
"""
    replay = command(root, sys.executable, "-B", "-X", "utf8", "-c", worker)
    assert b"C01_SOURCE_UNREACHABLE_REPLAY_PASS" in replay.stdout
    selectors = [
        "tests/research/test_five_year_frozen_baseline.py::test_original_research_evidence_and_factor_parameters_are_frozen",
        "tests/market_structure/test_delivery.py::test_protected_tracked_content_is_unchanged",
        "tests/evidence/test_pit_archive_verifiers.py::test_urls_relative_paths_and_dates_pass",
    ]
    history = command(
        root,
        sys.executable,
        "-B",
        "-m",
        "pytest",
        *selectors,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(tmp_path / "history-temp")
    )
    assert b"19 passed" in history.stdout
    assert command(root, "git", "status", "--porcelain").stdout == b""
    assert (
        command(
            root, "git", "cat-file", "-e", control.SOURCE + "^{commit}", check=False
        ).returncode
        != 0
    )
    proof = {
        "scenario": "SQUASH_FRESH_CLONE",
        "squash": squash,
        "source_cat_file_exit": absent.returncode,
        "alternates": False,
        "historical_tests": 19,
        "worker": json.loads(replay.stdout.decode().splitlines()[-1]),
    }
    request.node.user_properties.append(
        ("c01_source_unreachable_evidence", json.dumps(proof, sort_keys=True))
    )
    print(json.dumps(proof))


@pytest.mark.parametrize(
    "mutation",
    [
        "source_commit",
        "source_changes",
        "source_closure",
        "sha256",
        "blob",
        "path",
        "mode",
    ],
)
def test_untrusted_source_manifest_changes_are_rejected(candidate, control, mutation):
    path = candidate / control.REGISTRY
    value = json.loads(path.read_bytes())
    name = "qbot/research/data/csmar/native.py"
    if mutation == "source_commit":
        value["source_commit"] = "HEAD"
    elif mutation == "source_changes":
        value["source_changes"].pop(name)
    elif mutation == "source_closure":
        value["source_closure"].pop(name)
    elif mutation == "path":
        value["source_changes"][name + chr(47) + "../native.py"] = value[
            "source_changes"
        ].pop(name)
    else:
        value["source_changes"][name][mutation] = {
            "sha256": "0" * 64,
            "blob": "0" * 40,
            "mode": "120000",
        }[mutation]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="registry digest changed"):
        control.validate_preservation(candidate)
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller_at(ROOT).load_security_revision(candidate)


def test_registry_and_anchor_rehash_cannot_replace_trusted_workflow_pin(
    candidate, control
):
    path = candidate / control.REGISTRY
    value = json.loads(path.read_bytes())
    value["source_changes"]["qbot/research/data/csmar/native.py"]["sha256"] = "0" * 64
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(body)
    anchor = candidate / control.ANCHOR
    payload = json.loads(anchor.read_bytes())
    payload["files"][control.REGISTRY] = hashlib.sha256(body).hexdigest()
    anchor.write_bytes((json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller_at(ROOT).load_security_revision(candidate)


@pytest.mark.parametrize(
    "mutation", ["count", "noop", "path", "mode", "blob", "sha256", "inventory"]
)
def test_manifest_reconstruction_requires_exact_finite_consistent_entries(
    control, mutation
):
    value = copy.deepcopy(control.read_registry(ROOT))
    name = "qbot/research/data/csmar/native.py"
    if mutation == "count":
        value["source_changes"].pop(name)
    elif mutation == "noop":
        before, _ = control.source_inventory(ROOT, value)
        value["source_changes"]["qbot/research/factors/registry.py"] = before[
            "qbot/research/factors/registry.py"
        ]
        value["source_changes"].pop(name)
    elif mutation == "path":
        value["source_changes"]["../native.py"] = value["source_changes"].pop(name)
    elif mutation == "inventory":
        value["source_authentication"]["inventory_sha256"] = "0" * 64
    else:
        value["source_changes"][name][mutation] = {
            "mode": "120000",
            "blob": "0" * 40,
            "sha256": "0" * 64,
        }[mutation]
    with pytest.raises(ValueError):
        control.source_inventory(ROOT, value)


@pytest.mark.parametrize(
    "mutation",
    ["delete", "rename", "content", "migration", "git_mode", "runtime_bytes"],
)
def test_actual_file_and_mode_changes_are_rejected(candidate, control, mutation):
    name = "qbot/research/data/csmar/native.py"
    path = candidate / name
    if mutation == "delete":
        path.unlink()
    elif mutation == "rename":
        path.rename(path.with_name("replaced_native.py"))
    elif mutation == "git_mode":
        command(candidate, "git", "update-index", "--chmod=+x", name)
    elif mutation == "runtime_bytes":
        target = candidate / "configs/research/csi500_5y.yaml"
        raw = target.read_bytes()
        target.write_bytes(
            raw.replace(b"\r\n", b"\n")
            if b"\r\n" in raw
            else raw.replace(b"\n", b"\r\n")
        )
    else:
        if mutation == "migration":
            path = candidate / "examples/research/review_yhh30m_release.py"
        path.write_bytes(path.read_bytes() + b"\n# unauthorized content\n")
    with pytest.raises(ValueError):
        control.validate_preservation(candidate)


def test_tracked_symlink_mode_is_rejected_without_following_target(candidate, control):
    name = "qbot/research/data/csmar/native.py"
    oid = command(candidate, "git", "hash-object", name).stdout.decode().strip()
    command(
        candidate, "git", "update-index", "--cacheinfo", "120000," + oid + "," + name
    )
    with pytest.raises(ValueError, match="file mode mismatch"):
        control.validate_preservation(candidate)


def test_source_content_rejects_unregistered_or_changed_source(candidate, control):
    with pytest.raises(ValueError, match="Unknown C01 source path"):
        control.source_content(candidate, "qbot/research/unknown.py")
    path = candidate / "qbot/research/data/csmar/native.py"
    path.write_bytes(path.read_bytes() + b"\n# changed original literal inventory\n")
    with pytest.raises(ValueError, match="source content identity mismatch"):
        control.source_content(candidate, "qbot/research/data/csmar/native.py")


@pytest.mark.parametrize(
    "name",
    [
        ".github/scripts/ci/c01_preservation.py",
        ".github/workflows/market-admission.yml",
        pytest.param("docs/research/evidence/c01-status-v1/trust-anchor.json", id="docs/research/evidence/c01-solo-v1/trust-anchor.json"),
    ],
)
def test_control_file_modes_are_authenticated(candidate, control, name):
    command(candidate, "git", "update-index", "--chmod=+x", name)
    with pytest.raises(ValueError, match="file mode mismatch"):
        control.validate_preservation(candidate)
