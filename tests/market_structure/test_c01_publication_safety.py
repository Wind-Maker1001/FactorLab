"""C01 literal false positives are finite; real paths still block publication."""

from collections import Counter
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from examples.research import review_yhh30m_release as scanner
from tests.market_structure.test_minute_gate_compatibility import publication_repo, write  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "docs/research/evidence/c01-solo-v1/version-registry.json"


def collected_attacks():
    prefix = ("tests/research/test_csmar_source_binding.py::"
              "test_reject_malicious_zip_paths[")
    return [("|execution_delta|added|" + key, prefix + part + "evil]", part + "evil")
            for key in ("all_ids", "selected_ids") for part in (chr(47), "C:" + chr(47))]


def test_minimal_node_counterexample_contains_no_machine_test_path():
    matches = collected_attacks()
    assert len(matches) == 4
    assert Counter(m.group() for _, value, _ in matches for m in scanner.ABS.finditer(value)) == Counter(t for _, _, t in matches)
    revision = {"files": {REGISTRY: {}}}
    security = SimpleNamespace(REGISTRY=REGISTRY)
    assert scanner.c01_literal_inventory(ROOT, REGISTRY, revision, security, matches)
    assert not scanner.c01_literal_inventory(ROOT, REGISTRY, None, security, matches)


@pytest.mark.parametrize("mutation", ["extra", "field", "node", "parameter", "missing"])
def test_node_exception_cannot_authorize_other_content(mutation):
    matches = collected_attacks()
    context, value, token = matches[0]
    if mutation == "extra":
        matches.append((context, "C:" + chr(47) + "Users/alice/private", "C:" + chr(47) + "Users/alice/private"))
    elif mutation == "field":
        matches[0] = ("|private_location", value, token)
    elif mutation == "node":
        matches[0] = (context, value.replace("test_reject_malicious_zip_paths", "test_other"), token)
    elif mutation == "parameter":
        matches[0] = (context, value.replace("evil", "Users/alice/private"), token.replace("evil", "Users/alice/private"))
    else:
        matches.pop()
    assert not scanner.c01_literal_inventory(ROOT, REGISTRY, {"files": {REGISTRY: {}}}, SimpleNamespace(REGISTRY=REGISTRY), matches)


@pytest.mark.parametrize("suffix", ["json", "md", "py"])
@pytest.mark.parametrize("prefix", ["C:", "D:", "", "UNC"])
def test_real_paths_rejected_in_every_public_content_type(publication_repo, suffix, prefix):
    root, base = publication_repo
    path = ((chr(92) * 2 + "host" + chr(92) + "private") if prefix == "UNC"
            else prefix + chr(47) + "Users/alice/private")
    content = json.dumps({"path": path}) if suffix == "json" else "path = " + repr(path) if suffix == "py" else path
    name = "docs/research/unrelated/probe." + suffix
    write(root, name, content.encode())
    result = scanner.review(root, base)
    assert result["status"] == "FAIL"
    assert any(f["file"] == name and f["reason"] in {"MACHINE_PATH", "UNEXPLAINED_ABSOLUTE_PATH"} for f in result["blocked_findings"])


@pytest.mark.parametrize("name", [
    "qbot/research/data/csmar/binding.py",
    "qbot/research/data/csmar/fingerprint.py",
    "qbot/research/data/csmar/native.py",
    "tests/research/test_csmar_source_binding.py",
])
def test_exact_source_literal_inventory_rejects_added_paths(name):
    import ast
    spec = importlib.util.spec_from_file_location("publication_controller", ROOT / ".github/scripts/ci/research_checks.py")
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    security = controller.load_security_revision(ROOT)
    revision = security.load_revision(ROOT)
    values = [n.value for n in ast.walk(ast.parse((ROOT / name).read_text(encoding="utf-8-sig")))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    matches = [("PYTHON_STRING", value, m.group()) for value in values
               for m in scanner.ABS.finditer(scanner.URL.sub("REMOTE_URL", value)) if m.group() != chr(47)]
    assert scanner.c01_literal_inventory(ROOT, name, revision, security, matches)
    bad = "C:" + chr(47) + "Users/alice/private"
    assert not scanner.c01_literal_inventory(ROOT, name, revision, security, matches + [("PYTHON_STRING", bad, bad)])
