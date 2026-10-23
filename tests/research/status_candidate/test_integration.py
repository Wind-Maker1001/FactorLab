"""Cross-module STATUS integration using self-contained synthetic sources."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.evidence import snapshot_file
from factorlab.data.market_qualification import qualify_market_data
from factorlab.errors import ProviderError, SecurityStatusDataError
from factorlab.universe.historical_status import HistoricalStatus
from factorlab.universe.identity import IdentitySnapshot, SecurityIdentity
from factorlab.errors import DataQualityError
from factorlab.universe.status import validate_security_data
from factorlab.universe.status_candidate import EvidenceError
from factorlab.universe.status_candidate.ingest import sha256
from tests.research.market_data_fixture import qualification_inputs


def load(f):
    source = snapshot_file(f['path'], 'SYNTHETIC_STATUS_BUNDLE',
                           '2026-10-22T00:00:00+08:00')
    return HistoricalStatus.load(source, allow_synthetic=True)


def update(f):
    f['path'].write_text(json.dumps(f['bundle'], sort_keys=True), encoding='utf-8')
    f['sha'] = sha256(f['path'])
    return f


def query(adapter, date='2024-01-02', decision_at='2024-01-02T09:00:00+08:00'):
    return adapter.query('000001.SZ', date, decision_at=decision_at)


@pytest.mark.parametrize('state', ['NORMAL', 'ST', '*ST', 'OTHER'])
def test_explicit_st_dictionary_and_decision_unknown(make_bundle, state):
    a = load(make_bundle(official_patch={'field': 'ST_RISK', 'value': state},
                         native_patch={'native_state_code': 0}))
    result = query(a)
    assert result['facts']['st_risk_status'] == state
    assert result['facts']['suspension_status'] == 'NOT_SUSPENDED'
    assert result['decision_status']['st_risk_status'] == 'UNKNOWN'
    assert result['role_statuses']['STATUS'] == 'PARTIAL'
    assert result['role_statuses']['SOURCE_BINDING'] == 'SOURCE_BINDING_INCOMPLETE'
    assert not result['execution_admission']


def test_dated_normal_st_starst_transitions_without_filling(make_bundle):
    f = make_bundle(official=False)
    for i, (date, state) in enumerate(zip(['2024-01-02', '2024-01-03', '2024-01-04'],
                                         ['NORMAL', 'ST', '*ST'])):
        f['bundle']['official'].append(dict(f['official'], evidence_id='SYNTHETIC_'+str(i),
            field='ST_RISK', value=state, effective_from=date, effective_to=date,
            declared_dates=[date], event_date=date))
    a = load(update(f))
    assert [query(a, d)['facts']['st_risk_status'] for d in sorted(a.candidate.calendar)] == ['NORMAL', 'ST', '*ST']


@pytest.mark.parametrize('decision_at', ['2023-12-01T00:00:00+08:00',
    '2024-01-02T00:00:00+08:00', '2024-01-04T23:59:59+08:00',
    '2026-10-22T00:00:00+08:00'])
def test_announcement_effective_end_record_and_knowledge_are_distinct(make_bundle, decision_at):
    a = load(make_bundle(official_patch={'field': 'ST_RISK', 'value': '*ST',
        'announced_at': '2024-01-03', 'source_published_at': '2024-01-03',
        'event_date': '2024-01-04'}))
    r = query(a, decision_at=decision_at)
    e = r['facts']['official_evidence'][0]
    assert (e['announced_at'], e['effective_from'], e['effective_to'], e['event_date']) == (
        '2024-01-03', '2024-01-02', '2024-01-02', '2024-01-04')
    assert e['historical_known_at'] is None and e['first_available_at'] is None
    assert set(r['decision_status'].values()) == {'UNKNOWN'}
    assert r['historical_known_at'] == 'UNKNOWN'
    assert r['source_recorded_at'] == 'UNKNOWN'
    assert r['source_acquired_at'] != e['event_date']
    assert query(a, '2024-01-03')['facts']['st_risk_status'] == 'UNKNOWN'


@pytest.mark.parametrize('date', ['2024-01-02', '2024-01-03', '2024-01-04'])
def test_no_first_or_termination_event_does_not_imply_normal(make_bundle, date):
    a = load(make_bundle(official=False))
    assert query(a, date)['facts']['st_risk_status'] == 'UNKNOWN'


def test_conflict_and_duplicate_never_overwrite(make_bundle):
    f = make_bundle(official_patch={'field': 'ST_RISK', 'value': 'ST'})
    other = dict(f['official'], evidence_id='SYNTHETIC_OTHER', value='NORMAL')
    f['bundle']['official'].append(other)
    a = load(update(f)); result = query(a)
    assert result['facts']['st_risk_status'] == 'UNKNOWN'
    assert result['facts']['conflict_fields'] == ['ST_RISK']
    assert result['facts']['source_conflict_status'] == 'SOURCE_CONFLICT'
    f['bundle']['official'].append(other)
    with pytest.raises(EvidenceError, match='DUPLICATE_EVIDENCE_ID'):
        load(update(f))


@pytest.mark.parametrize('symbol', ['000001', '000001.BJ', '000001.sh', ' 000001.SZ', 'XYZ'])
def test_invalid_identity_refused(make_bundle, symbol):
    with pytest.raises(EvidenceError, match='INVALID_CANONICAL_SECURITY'):
        load(make_bundle(native_patch={'security': symbol}))


def test_changed_source_or_bundle_refused_on_actual_query(make_bundle):
    f = make_bundle(); a = load(f)
    f['source'].write_bytes(b'ALTERED')
    with pytest.raises(EvidenceError, match='SOURCE_SHA_MISMATCH'):
        query(a)


def test_native_unknown_and_deterministic_provenance(make_bundle):
    a = load(make_bundle(official=False))
    before = query(a); after = query(a)
    assert before == after
    assert before['facts']['native_observation']['native_trdsta'] == 10
    assert before['facts']['st_risk_status'] == 'UNKNOWN'
    assert before['source_sha256'] == a.source.sha256
    assert before['role_statuses']['PIT'] == 'PIT_UNVERIFIED'


def test_real_status_validator_qualification_and_admission_refuse(make_bundle, tmp_path):
    a = load(make_bundle(official=False, symbol='600000.SH'))
    inputs, keys, dates, stocks, benchmark = qualification_inputs(tmp_path, periods=3)
    frame = a.status_frame(keys[['symbol', 'date']], decision_at='2024-01-04T15:00:00+08:00')
    assert list(frame.columns) == ['date', 'symbol', 'trade_status', 'is_st']
    assert frame[['trade_status', 'is_st']].isna().all().all()
    with pytest.raises(SecurityStatusDataError, match='must not be null'):
        validate_security_data(frame, inputs.listings, calendar=dates, membership=inputs.membership)
    report = qualify_market_data(replace(inputs, status=frame, status_source=a.source),
        keys, dates, stocks=stocks, benchmark=benchmark)
    assert report.stages['STATUS'] == 'INCOMPLETE'
    assert not report.formal_admission and not report.execution_admission
    with pytest.raises(ProviderError, match='qualification'):
        assert_formal_market_admission(SimpleNamespace(), membership=inputs.membership,
            calendar=dates, bars=stocks, benchmark=benchmark,
            research_start=inputs.research_start, research_end=inputs.research_end,
            status=frame, listings=inputs.listings)


@pytest.mark.parametrize('change', ['unknown_enum', 'missing_end', 'reversed_dates', 'duplicate_native'])
def test_malformed_source_contract_refused(make_bundle, change):
    if change == 'unknown_enum':
        f = make_bundle(official_patch={'field': 'ST_RISK', 'value': 'SUPPLIER_CODE_10'})
    elif change == 'missing_end':
        f = make_bundle(official_patch={'effective_to': None})
    elif change == 'reversed_dates':
        f = make_bundle(official_patch={'effective_to': '2024-01-01'})
    else:
        f = make_bundle(); p = f['path'].parent/'native.json'
        p.write_text(json.dumps([f['native'], f['native']]), encoding='utf-8')
        next(x for x in f['bundle']['artifacts'] if x['source_id']=='NATIVE')['sha256'] = sha256(p)
        update(f)
    with pytest.raises(EvidenceError):
        load(f)


def test_synthetic_layout_cannot_enter_real_loader(make_bundle):
    f = make_bundle()
    source = snapshot_file(f['path'], 'SYNTHETIC_STATUS', '2026-10-22T00:00:00+08:00')
    with pytest.raises(EvidenceError, match='SYNTHETIC_INPUT_NOT_REAL_ADMISSION'):
        HistoricalStatus.load(source)


@pytest.mark.parametrize('case', ['valid', 'gap', 'future', 'altered', 'ambiguous'])
def test_existing_identity_resolution_and_source_checks(make_bundle, tmp_path, case):
    f = make_bundle(); source = snapshot_file(f['path'], 'SYNTHETIC_STATUS',
                                               '2026-10-22T00:00:00+08:00')
    p = tmp_path/'identity.json'; p.write_text('{"synthetic_identity":true}', encoding='utf-8')
    artifact = snapshot_file(p, 'SYNTHETIC_IDENTITY', '2026-10-22T00:00:00+08:00')
    item = SecurityIdentity('synthetic-stable-security', '000001.SZ',
        '2024-01-03' if case == 'gap' else '2024-01-02', '2024-01-04',
        '2020-01-01', '2024-01-03' if case == 'future' else '2023-12-31',
        artifact.sha256, security_type='A_SHARE')
    identity = IdentitySnapshot((item, item) if case == 'ambiguous' else (item,),
                                (artifact,), synthetic_only=True)
    if case == 'ambiguous':
        with pytest.raises(DataQualityError, match='overlapping'):
            HistoricalStatus.load(source, identity=identity, allow_synthetic=True)
        return
    a = HistoricalStatus.load(source, identity=identity, allow_synthetic=True)
    if case == 'altered':
        p.write_bytes(b'altered')
    if case == 'valid':
        result = query(a)
        assert result['security_id'] == 'synthetic-stable-security'
        assert result['role_statuses']['IDENTITY'] == 'PARTIAL'
        assert set(result['decision_status'].values()) == {'UNKNOWN'}
    else:
        with pytest.raises(DataQualityError):
            query(a)
