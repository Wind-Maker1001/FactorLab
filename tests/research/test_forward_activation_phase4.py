"""Forward activation must be immutable and registered before it starts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import inspect

import pytest

import factorlab.forward.activation as activation_module
from factorlab.forward.activation import (
    ForwardActivationError,
    load_forward_activation,
    register_forward_activation,
)
from factorlab.forward.integrity import (
    ForwardIntegrityError,
    ForwardWorkflowLockError,
    research_source_sha256,
    workflow_lock,
)


CONTRACT = "a" * 64
SOURCE = "c" * 64
COMMIT = "b" * 40
NOW = datetime(2026, 9, 23, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fixed_registration_clock(monkeypatch):
    monkeypatch.setattr(activation_module, "_utc_now", lambda: NOW)


def _register(root):
    return register_forward_activation(
        root,
        activation_date=date(2026, 9, 24),
        contract_sha256=CONTRACT,
        source_sha256=SOURCE,
        git_commit=COMMIT,
        config_path="configs/research/csi500_forward.yaml",
    )


def test_activation_is_registered_in_advance_and_verified(tmp_path):
    receipt = _register(tmp_path)
    loaded = load_forward_activation(
        tmp_path,
        receipt.activation_id,
        expected_contract_sha256=CONTRACT,
        expected_source_sha256=SOURCE,
    )
    assert loaded.activation_date == date(2026, 9, 24)
    assert loaded.registered_at == NOW
    assert loaded.source_sha256 == SOURCE
    assert loaded.path == receipt.path
    assert len(loaded.manifest_sha256) == 64


@pytest.mark.parametrize("day", [date(2026, 9, 22), date(2026, 9, 23)])
def test_activation_rejects_past_or_same_market_date(tmp_path, day):
    with pytest.raises(ForwardActivationError, match="strictly after"):
        register_forward_activation(
            tmp_path,
            activation_date=day,
            contract_sha256=CONTRACT,
            source_sha256=SOURCE,
            git_commit=COMMIT,
            config_path="config.yaml",
        )


def test_activation_never_overwrites_an_existing_receipt(tmp_path):
    receipt = _register(tmp_path)
    original = (receipt.path / "manifest.json").read_bytes()
    with pytest.raises(ForwardActivationError, match="already exists"):
        _register(tmp_path)
    assert (receipt.path / "manifest.json").read_bytes() == original


def test_activation_tampering_and_contract_mismatch_fail(tmp_path):
    receipt = _register(tmp_path)
    with pytest.raises(ForwardActivationError, match="contract"):
        load_forward_activation(
            tmp_path,
            receipt.activation_id,
            expected_contract_sha256="c" * 64,
        )
    with pytest.raises(ForwardActivationError, match="source"):
        load_forward_activation(
            tmp_path,
            receipt.activation_id,
            expected_source_sha256="d" * 64,
        )
    (receipt.path / "manifest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ForwardActivationError, match="SHA-256"):
        load_forward_activation(tmp_path, receipt.activation_id)


def test_activation_clock_cannot_be_supplied_by_caller():
    assert "registered_at" not in inspect.signature(register_forward_activation).parameters


def test_activation_rejects_symlinked_evidence_ancestor(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this platform")
    with pytest.raises(ForwardActivationError, match="symlink|reparse"):
        _register(linked / "forward")


def test_research_source_digest_is_deterministic_and_content_bound(tmp_path):
    source = tmp_path / "src" / "factorlab"
    requirements = tmp_path / "pyproject.toml"
    source.mkdir(parents=True)
    requirements.parent.mkdir(parents=True)
    (source / "b.py").write_text("B = 2\n", encoding="utf-8")
    (source / "a.py").write_text("A = 1\n", encoding="utf-8")
    requirements.write_text("pandas==2.2.3\n", encoding="utf-8")

    first = research_source_sha256(tmp_path)
    assert first == research_source_sha256(tmp_path)
    (source / "a.py").write_text("A = 3\n", encoding="utf-8")
    assert research_source_sha256(tmp_path) != first


def test_research_source_digest_rejects_linked_source_file(tmp_path):
    source = tmp_path / "src" / "factorlab"
    requirements = tmp_path / "pyproject.toml"
    source.mkdir(parents=True)
    requirements.parent.mkdir(parents=True)
    requirements.write_text("pytest==8.3.5\n", encoding="utf-8")
    target = tmp_path / "outside.py"
    target.write_text("SECRET = True\n", encoding="utf-8")
    try:
        (source / "linked.py").symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this platform")
    with pytest.raises(ForwardIntegrityError, match="symlink|reparse"):
        research_source_sha256(tmp_path)


def test_workflow_lock_is_exclusive_and_never_auto_deleted(tmp_path):
    lock_path = tmp_path / "locks" / "forward.lock"
    with workflow_lock(lock_path):
        assert lock_path.is_file()
        with pytest.raises(ForwardWorkflowLockError, match="already held"):
            with workflow_lock(lock_path):
                pass
        assert lock_path.is_file()
    assert not lock_path.exists()


def test_workflow_lock_never_deletes_a_replaced_owner_file(tmp_path):
    lock_path = tmp_path / "locks" / "forward.lock"
    with pytest.raises(ForwardWorkflowLockError, match="ownership changed"):
        with workflow_lock(lock_path):
            lock_path.write_text("pid=999\ntoken=another-owner\n", encoding="ascii")
    assert lock_path.read_text(encoding="ascii").endswith("another-owner\n")


def test_concurrent_activation_has_exactly_one_winner(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(tmp_path), range(2)))
    assert sorted(results) == ["error", "success"]
    activation_dirs = [
        path for path in (tmp_path / "activations").iterdir() if path.is_dir()
    ]
    assert len(activation_dirs) == 1
    assert not list((tmp_path / "activations").glob("*.lock"))


def _attempt(root):
    try:
        _register(root)
    except ForwardActivationError:
        return "error"
    return "success"
