"""Pinned historical receipts in complete and shallow candidate repositories."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def audit():
    spec = importlib.util.spec_from_file_location('d07_git_receipt', ROOT / 'tools/data/audit_p3_d07.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'candidate'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'remote', 'add', 'origin', ROOT.as_uri())
    subject = git(ROOT, 'rev-parse', 'HEAD')
    git(repo, 'fetch', '--no-tags', '--depth=2', 'origin', subject)
    subprocess.check_call(
        ['git', '-C', str(repo), 'checkout', '--detach', subject],
        env={**os.environ, 'GIT_LFS_SKIP_SMUDGE': '1'},
    )
    git(repo, 'config', 'user.name', 'Receipt fixture')
    git(repo, 'config', 'user.email', 'receipt@example.invalid')
    return repo


def test_shallow_and_complete_receipts_match(audit, repository):
    assert git(repository, 'rev-parse', '--is-shallow-repository') == 'true'
    subject = git(repository, 'rev-parse', 'HEAD')
    shallow = audit.d06_reference(audit.D06_COMMIT, repository=repository)
    assert git(repository, 'rev-parse', '--is-shallow-repository') == 'false'
    assert git(repository, 'rev-parse', 'HEAD') == subject
    assert shallow == audit.d06_reference(audit.D06_COMMIT, repository=repository)
    assert shallow['admitted_member_days'] == 0
    assert shallow['artifact_sha256'] == audit.D06_ARTIFACTS


def test_wrong_revision_rejected_before_fetch(audit, repository):
    with pytest.raises(ValueError, match='fixed merged commit'):
        audit.d06_reference('HEAD', repository=repository)
    assert git(repository, 'rev-parse', '--is-shallow-repository') == 'true'


def test_unavailable_origin_rejected(audit, repository):
    git(repository, 'remote', 'set-url', 'origin', (repository / 'missing.git').as_uri())
    with pytest.raises(subprocess.CalledProcessError):
        audit.d06_reference(audit.D06_COMMIT, repository=repository)


def test_missing_object_in_complete_repository_rejected(audit, tmp_path):
    git(tmp_path, 'init')
    assert git(tmp_path, 'rev-parse', '--is-shallow-repository') == 'false'
    with pytest.raises(subprocess.CalledProcessError):
        audit.d06_reference(audit.D06_COMMIT, repository=tmp_path)


def test_nonancestor_rejected(audit, repository):
    audit.d06_reference(audit.D06_COMMIT, repository=repository)
    git(repository, 'checkout', '--detach', audit.D06_COMMIT + '^')
    with pytest.raises(subprocess.CalledProcessError) as failure:
        audit.d06_reference(audit.D06_COMMIT, repository=repository)
    assert failure.value.returncode == 1


@pytest.mark.parametrize('change', ['tamper', 'missing'])
def test_pinned_evidence_rejected(audit, repository, monkeypatch, change):
    audit.d06_reference(audit.D06_COMMIT, repository=repository)
    path = repository / 'docs/validation/p3-d06/factors.csv'
    if change == 'tamper':
        path.write_text('tampered\n', encoding='utf-8')
    else:
        path.unlink()
    git(repository, 'add', str(path))
    git(repository, 'commit', '-m', 'Invalid receipt fixture')
    monkeypatch.setattr(audit, 'D06_COMMIT', git(repository, 'rev-parse', 'HEAD'))
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        audit.d06_reference(audit.D06_COMMIT, repository=repository)
