"""STATUS version registration preserves predecessor controls and rejections."""

import hashlib
from pathlib import Path
import subprocess

import pytest

from qbot.research.diagnostics.model_readiness.source_identity import source_snapshot
from tests.research.test_c01_preservation import candidate, control  # noqa: F401
from tests.market_structure.test_c01_publication_safety import collected_attacks
from examples.research.review_yhh30m_release import c01_literal_inventory


ROOT = Path(__file__).resolve().parents[3]


def test_prior_control_versions_and_source_closure_remain_exact(control):
    value = control.validate_preservation(ROOT)
    assert value['version'] == 'STATUS_ADAPTER_SOLO_PRESERVATION_V1'
    for name in ('version-registry.json', 'trust-anchor.json'):
        relative = 'docs/research/evidence/c01-solo-v1/' + name
        old = subprocess.check_output(['git', 'show',
            'e442b7cc1d17c18d6e61f49692ad8decc27dca63:' + relative], cwd=ROOT)
        assert (ROOT / relative).read_bytes() == old
    assert len(value['source_closure']) == 35
    assert len(value['source_changes']) == 29
    assert not value['financial_recertification']


def test_source_identity_includes_the_integrated_adapter():
    snapshot = source_snapshot(ROOT)
    paths = {r['path'] for r in snapshot['files']}
    assert 'qbot/research/universe/historical_status.py' in paths
    assert 'qbot/research/universe/status_candidate/adapter.py' in paths
    assert snapshot['canonical_sha256'] == snapshot['git_canonical_sha256']


@pytest.mark.parametrize('mutation', ['source', 'newline', 'predecessor'])
def test_status_or_historical_control_change_is_rejected(candidate, control, mutation):
    name = ('docs/research/evidence/c01-solo-v1/trust-anchor.json' if mutation == 'predecessor'
            else 'qbot/research/universe/status_candidate/adapter.py')
    path = candidate / name
    body = path.read_bytes()
    path.write_bytes(body.replace(b'\n', b'\r\n') if mutation == 'newline' else body+b'\n# altered\n')
    with pytest.raises(ValueError, match='C01 STATUS'):
        control.validate_preservation(candidate)


def test_old_version_cannot_be_selected_by_new_package(control):
    with pytest.raises(ValueError, match='Unregistered C01 version'):
        control.validate_preservation(ROOT, 'CSMAR_RAW_SOLO_PRESERVATION_V2')


@pytest.mark.parametrize('version', ['current', 'predecessor'])
def test_versioned_registry_literals_remain_finite(control, version):
    name = (control.REGISTRY if version == 'current'
            else 'docs/research/evidence/c01-solo-v1/version-registry.json')
    revision = control.load_revision(ROOT)
    attacks = collected_attacks()
    assert c01_literal_inventory(ROOT, name, revision, control, attacks)
    assert not c01_literal_inventory(ROOT, name, revision, control, attacks + [attacks[0]])
    assert not c01_literal_inventory(ROOT, name+'-unregistered', revision, control, attacks)
