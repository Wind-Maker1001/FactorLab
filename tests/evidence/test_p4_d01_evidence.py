"""Negative evidence cases do not manufacture historical qualification."""
import csv
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('p4d01', ROOT / 'tools/evidence/audit_p4_d01.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
BUNDLE = ROOT / 'docs/validation/p4-d01/bundle'


def evidence():
    with (BUNDLE / 'EVIDENCE_MATRIX.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    return rows, json.loads((BUNDLE / 'evidence-index.json').read_bytes())


def test_committed_bundle_binds_bytes_and_upstream_sources():
    result = audit.verify_bundle(BUNDLE)
    assert result['scope'] == 'LOCAL_ENGINEERING_EVIDENCE_BINDINGS_ONLY'
    assert result['matrix_counts']['UNKNOWN'] > 0
    obs = json.loads((BUNDLE / 'local-observations.json').read_bytes())
    d07 = json.loads((ROOT / 'docs/validation/p3-d07/real-multisource.json').read_bytes())
    audit.validate_observations(obs, d07)
    assert len(obs['cross_source']['differences']) == 4
    assert not obs['cross_source']['merged']


@pytest.mark.parametrize('mutation', ['version', 'available', 'merge', 'formal', 'period'])
def test_mixed_versions_export_time_and_fusion_rejected(mutation):
    obs = json.loads((BUNDLE / 'local-observations.json').read_bytes())
    if mutation == 'version':
        obs['sources']['csmar']['source']['data_version'] = 'other-vintage'
    elif mutation == 'available':
        obs['sources']['bigquant']['source']['provider_available_at'] = '2026-11-07T12:22:03+00:00'
    elif mutation == 'merge':
        obs['cross_source']['merged'] = True
    elif mutation == 'period':
        obs['sources']['csmar']['query']['start'] = '2021-09-13'
    else:
        obs['eligibility']['formal_admission']['status'] = 'PASS'
    with pytest.raises(ValueError):
        audit.validate_observations(obs, json.loads((ROOT / 'docs/validation/p3-d07/real-multisource.json').read_bytes()))


def test_same_version_label_does_not_hide_changed_shard_receipt():
    obs = json.loads((BUNDLE / 'local-observations.json').read_bytes())
    d07 = json.loads((ROOT / 'docs/validation/p3-d07/real-multisource.json').read_bytes())
    obs['sources']['bigquant']['source']['provenance']['selected_archive_binding']['members'][0]['receipt_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='shard/receipt identity'):
        audit.validate_observations(obs, d07)


@pytest.mark.parametrize('condition', [
    'PIT_membership', 'PIT_first_distribution', 'HFQ_algorithm', 'HFQ_absolute_anchor',
    'HFQ_independent_reference', 'EVENT_completeness_rights', 'EVENT_completeness_split_merge',
    'RAW_exact_cross_source_values', 'FORMAL_admission',
])
def test_insufficient_and_conflicting_evidence_never_passes(condition):
    rows, index = evidence()
    target = next(row for row in rows if row['condition'] == condition)
    target['status'] = 'PASS'
    with pytest.raises(ValueError):
        audit.validate_rows(rows, index)


@pytest.mark.parametrize('field,value', [
    ('content_sha256', '0' * 64), ('source_version', 'new-version'),
    ('known_at', '2018-06-13'), ('effective_at', '2018-06-13'),
])
def test_original_binding_and_announcement_implementation_separation(field, value):
    rows, index = evidence()
    target = next(row for row in rows if row['condition'] == 'EVENT_cash_terms' and row['date_or_period'] == '2018-06-21')
    assert target['known_at'] == 'UNKNOWN'
    assert target['announced_at'].startswith('2018-06-13')
    target[field] = value
    with pytest.raises(ValueError):
        audit.validate_rows(rows, index)


def test_challenge_page_is_not_official_pdf():
    rows, index = evidence()
    target = next(row for row in rows if row['condition'] == 'EVENT_original_official_pdf' and row['date_or_period'] == '2017-06-22')
    target['status'] = 'PASS'
    with pytest.raises(ValueError, match='original bytes'):
        audit.validate_rows(rows, index)


def test_published_bundle_verification_does_not_require_git_history(monkeypatch):
    def no_history(*args, **kwargs):
        raise RuntimeError('shallow checkout has no base object')
    monkeypatch.setattr(audit.subprocess, 'check_output', no_history)
    assert audit.verify_bundle(BUNDLE)['status'] == 'PASS'


def test_artifact_tampering_is_detected(tmp_path):
    target = tmp_path / 'bundle'
    shutil.copytree(BUNDLE, target)
    with (target / 'local-observations.json').open('ab') as stream:
        stream.write(b' ')
    with pytest.raises(ValueError, match='artifact hash mismatch'):
        audit.verify_bundle(target)


@pytest.mark.parametrize('complete,conflict,applicable,expected', [
    (True, False, True, 'PASS'), (False, False, True, 'UNKNOWN'),
    (True, True, True, 'FAIL'), (False, False, False, 'NOT_APPLICABLE'),
])
def test_local_classification(complete, conflict, applicable, expected):
    assert audit.classify(complete=complete, conflict=conflict, applicable=applicable) == expected


@pytest.mark.parametrize('checkout', [b'first\nsecond\n', b'first\r\nsecond\r\n'])
def test_git_blob_identity_survives_checkout_newlines(tmp_path, monkeypatch, checkout):
    (tmp_path / 'source.md').write_bytes(checkout)
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    monkeypatch.setattr(audit.subprocess, 'check_output', lambda *args, **kwargs: b'first\nsecond\n')
    assert audit.repository_bytes('source.md') == b'first\nsecond\n'
    (tmp_path / 'source.md').write_bytes(b'changed\n')
    with pytest.raises(ValueError, match='differs from pinned base'):
        audit.repository_bytes('source.md')


def test_real_formal_gate_still_rejects_evidence_inventory():
    from factorlab.data.admission import assert_formal_market_admission
    from factorlab.errors import ProviderError
    with pytest.raises(ProviderError, match='qualification'):
        assert_formal_market_admission(object(), membership=None, calendar=None, bars=None,
            benchmark=None, research_start='2018-01-02', research_end='2018-12-28')
