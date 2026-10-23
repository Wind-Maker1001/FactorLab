"""New migration counterexamples. No data qualification is issued by these tests."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab import cli
from factorlab._identity import software_identity
from factorlab.data import admission
from factorlab.demo import build_inputs, run
from factorlab.errors import FactorLabError, ProviderError
from tests.market_admission.test_admission import package, request as admission_request
from tests.research.test_cli_phase4 import _config, _Run


def test_migration_refuses_even_a_forged_all_pass_internal_report(package, monkeypatch):
    original = admission.load_qualification

    def compromised_loader(*args, **kwargs):
        report, *rest = original(*args, **kwargs)
        # Adversarial in-memory stub only: no receipt or source is modified.
        report = replace(report, synthetic_only=False, formal_admission=True,
                         execution_admission=True, issues=(),
                         stages={name:'PASS' for name in ('RAW','ADJUSTMENT','STATUS','PIT','SOURCE_BINDING','FORMAL')})
        return (report, *rest)

    monkeypatch.setattr(admission, 'load_qualification', compromised_loader)
    provider = SimpleNamespace(get_research_qualification=lambda: package[-1])
    with pytest.raises(ProviderError, match='MIGRATION_TRUST_ROOT_UNREVIEWED'):
        admission.assert_formal_market_admission(provider, **admission_request(package))


def test_forward_cannot_hash_a_different_tree_as_the_executing_package(tmp_path, monkeypatch):
    config = _config(tmp_path)
    object.__setattr__(config, 'repo_root', tmp_path/'impostor-source')
    # Isolate the additional physical source binding from Git's own refusal.
    monkeypatch.setattr(cli, '_assert_current_forward_worktree', lambda *a: None)
    with pytest.raises(FactorLabError, match='does not match the executing'):
        cli._forward_identity(config, _Run())


def test_supplied_real_or_changed_files_cannot_be_stamped_synthetic(tmp_path):
    inputs = build_inputs()
    inputs['bars'].loc[0,'close'] *= 2
    directory = tmp_path/'untrusted'
    directory.mkdir()
    for name, frame in inputs.items():
        frame.to_parquet(directory/(name+'.parquet'), index=False)
    output = tmp_path/'output'
    with pytest.raises(AssertionError):
        run(output, input_root=directory)
    assert not (output/'manifest.json').exists()


def test_new_identity_is_explicitly_non_authorizing_and_includes_resources():
    identity = software_identity()
    assert identity['protocol']=='FACTORLAB_NAMESPACE_MIGRATION_V1'
    assert identity['identity_is_authorization'] is False
    assert identity['trust_root_review']=='BLOCKED_INDEPENDENT_REVIEW_REQUIRED'
    assert identity['resource_sha256']['resources/csi500_5y.yaml']
    assert all(name.startswith('src/factorlab/') for name in identity['source_sha256'])
