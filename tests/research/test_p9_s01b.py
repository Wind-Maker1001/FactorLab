"""Bounded issuer status without execution or valuation permission."""
import json

import pandas as pd
import pytest

from factorlab.data import market_evidence_sidecar as sidecar
from factorlab.data.evidence import sha256
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.oos_execution_recovery import apply_issuer_execution_st
from factorlab.research.oos_execution_study import execution_bars


@pytest.fixture
def bounded(tmp_path, monkeypatch):
    designation, suspension, manifest = b'issuer 2023-36', b'issuer 2023-56', b'manifest'
    monkeypatch.setattr(sidecar, 'ST_EXECUTION_NOTICE_SHA256', sha256(designation))
    monkeypatch.setattr(sidecar, 'BOUNDED_SUSPENSION_SHA256', sha256(suspension))

    def load(day='2023-05-22', symbol='000540.SZ', **changes):
        suspension_fact = dict(security=symbol, start_date='2023-05-19', end_exclusive='2023-06-15',
            publication_time='2023-05-19T23:59:59.999999+08:00',
            document_sha256=sha256(suspension), verification_status='DOCUMENT_CONFIRMED')
        suspension_fact.update(changes)
        row = dict(target_security=symbol, target_date=day, field='is_st', original_value=None,
            candidate_value=True, evidence_class='ISSUER_RISK_DESIGNATION', source_uri='test://2023-36',
            source_document_sha256=sha256(designation), source_publication_time='2023-04-29',
            known_at_status=sidecar.ST_EXECUTION_KNOWN, retrospectively_verified_at='2026-11-12',
            verification_status='DOCUMENT_CONFIRMED', consumption_scope='RESEARCH_CANDIDATE_ONLY',
            limitation='Bounded execution state only', suspension_evidence=suspension_fact)
        path = tmp_path / 'state.json'
        path.write_text(json.dumps(dict(schema=sidecar.SCHEMA, version=1, safety=list(sidecar.SAFETY),
            source_manifest_sha256=sha256(manifest), candidates=[row])), encoding='utf8')
        return sidecar.load_sidecar(path, source_manifest=manifest,
            originals={(symbol, day, 'is_st'): None},
            documents={sha256(designation): designation, sha256(suspension): suspension})
    return load


@pytest.mark.parametrize('day', ['2023-05-22', '2023-06-14'])
def test_bounded_state_cutoff_and_no_valuation(bounded, day):
    evidence = bounded(day)
    assert evidence.fact('000540.SZ', day, 'is_st', purpose='EXECUTION_STATE',
                         cutoff=f'{day}T09:30:00+08:00').candidate_value is True
    with pytest.raises(DataQualityError):
        evidence.fact('000540.SZ', day, 'is_st', purpose='EXECUTION_STATE', cutoff='2023-04-28')
    with pytest.raises(DataQualityError, match='not public'):
        evidence.fact('000540.SZ', day, 'is_st', purpose='EXECUTION_STATE', cutoff='2023-05-19T09:30:00+08:00')
    with pytest.raises(DataQualityError):
        evidence.fact('000540.SZ', day, 'is_st', purpose='SIGNAL_STATE', cutoff=day)
    with pytest.raises(DataQualityError):
        evidence.fact('000540.SZ', day, 'valuation_mark')


@pytest.mark.parametrize('day,symbol', [('2023-05-19', '000540.SZ'),
    ('2023-06-15', '000540.SZ'), ('2023-05-22', '400174'), ('2023-05-22', '000541.SZ')])
def test_scope_refused(bounded, day, symbol):
    with pytest.raises(DataQualityError):
        bounded(day, symbol)


@pytest.mark.parametrize('changes', [dict(end_exclusive='2023-06-16'),
    dict(publication_time='2023-06-16'), dict(security='400174'),
    dict(document_sha256='0' * 64), dict(verification_status='UNKNOWN')])
def test_suspension_evidence_refused(bounded, changes):
    with pytest.raises(DataQualityError):
        bounded(**changes)


@pytest.mark.parametrize('suspended,close', [(0, None), (None, None), (1, .4)])
def test_changed_source_refused(bounded, suspended, close):
    bars = pd.DataFrame([dict(date=pd.Timestamp('2023-05-22'), symbol='000540.SZ',
        raw_open=None, raw_close=close, volume=0, suspended=suspended, st_status=2)])
    with pytest.raises(SchemaError, match='BOUNDED_SUSPENSION_SOURCE_DRIFT'):
        apply_issuer_execution_st(execution_bars(bars), bounded())


def test_source_fields_preserved_and_unqualified_state_refused(bounded):
    bars = pd.DataFrame([dict(date=pd.Timestamp('2023-05-22'), symbol='000540.SZ',
        raw_open=None, raw_close=None, volume=0, suspended=1, st_status=2)])
    original = execution_bars(bars)
    qualified = apply_issuer_execution_st(original, bounded())
    pd.testing.assert_frame_equal(qualified[bars.columns], bars, check_exact=True)
    assert pd.isna(original.is_st.item())
    assert qualified.is_st.item() == 1
