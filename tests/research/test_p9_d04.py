"""Exact-window grants must not propagate into raw prices or trading state."""
from decimal import Decimal
import json

import pytest

from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import SCHEMA, SAFETY, POLICY, SCOPED_SUSPENSIONS, load_sidecar
from factorlab.errors import DataQualityError


@pytest.fixture(params=list(SCOPED_SUSPENSIONS))
def scoped(request, tmp_path):
    symbol = request.param
    observed, start, resume, days = SCOPED_SUSPENSIONS[symbol]
    source, suspension, resumption = b'price shard', b'issuer suspension', b'issuer resumption'
    observation = dict(security=symbol, date=observed, field='raw_close',
        status='OBSERVED_RAW_CLOSE', price='10.25', volume=10000, source_sha256=sha256(source))
    capsule = json_bytes(observation)
    documents = {sha256(d): d for d in (source, suspension, resumption, capsule)}
    rows = []
    for day in days:
        rows.append(dict(target_security=symbol, target_date=day, field='valuation_mark',
            original_value=None, candidate_value='10.25', evidence_class='MODEL_VALUATION_ASSUMPTION',
            source_uri='synthetic://raw-observation', source_document_sha256=sha256(capsule),
            source_publication_time=observed, known_at_status='EX_POST_ONLY',
            retrospectively_verified_at='2026-11-12', verification_status='OBSERVATION_IDENTITY_BOUND',
            consumption_scope='RESEARCH_CANDIDATE_ONLY', limitation='No raw price or trading permission',
            valuation_policy=POLICY, valuation_policy_version='1', valuation_status='MODEL_VALUATION_ASSUMPTION',
            nontradable=True, valuation_source_date=observed, valuation_source_price='10.25',
            valuation_source_price_identity=sha256(source), suspension_evidence=dict(security=symbol,
                start_date=start, resume_date=resume, document_sha256=sha256(suspension),
                publication_time=observed, evidence_class='ISSUER_CONFIRMED_SUSPENSION',
                verification_status='DOCUMENT_CONFIRMED'),
            resumption_evidence=dict(security=symbol, resume_date=resume, document_sha256=sha256(resumption),
                                     verification_status='DOCUMENT_CONFIRMED')))
    payload = dict(schema=SCHEMA, version=1, safety=list(SAFETY), source_manifest_sha256=sha256(b'manifest'), candidates=rows)
    originals = {(symbol, day, 'valuation_mark'): None for day in days}
    def load():
        path = tmp_path / 'sidecar.json'
        path.write_bytes(json_bytes(payload))
        return load_sidecar(path, source_manifest=b'manifest', originals=originals, documents=documents)
    return symbol, observation, days, resume, payload, documents, load


def test_exact_days_and_unchanged_raw(scoped):
    symbol, observation, days, _, _, _, load = scoped
    evidence = load()
    for day in days:
        value = evidence.carry(symbol, day, shares=100, raw_close=None, previous_observation=observation)
        assert value.book_value == Decimal('1025.00')
        assert value.raw_close is None and value.nontradable
        assert value.valuation_status == 'MODEL_VALUATION_ASSUMPTION'
        with pytest.raises(DataQualityError):
            evidence.fact(symbol, day, 'valuation_mark', purpose='EXECUTION')
        with pytest.raises(DataQualityError):
            evidence.fact(symbol, day, 'is_st')


@pytest.mark.parametrize('mutation', ['suspension_bytes', 'resume_bytes', 'resume_date', 'start_date',
                                      'observation_date', 'price', 'raw_close', 'version', 'known_at'])
def test_scope_and_document_drift_refused(scoped, mutation):
    _, _, _, _, payload, documents, load = scoped
    row = payload['candidates'][0]
    if mutation == 'suspension_bytes':
        documents[row['suspension_evidence']['document_sha256']] = b'drift'
    elif mutation == 'resume_bytes':
        documents[row['resumption_evidence']['document_sha256']] = b'drift'
    elif mutation == 'resume_date':
        row['resumption_evidence']['resume_date'] = '2026-11-12'
    elif mutation == 'start_date':
        row['suspension_evidence']['start_date'] = row['target_date'] + 'x'
    elif mutation == 'observation_date':
        row['valuation_source_date'] = row['target_date']
    elif mutation == 'price':
        row['candidate_value'] = '11'
    elif mutation == 'raw_close':
        row['original_value'] = 10.25
    elif mutation == 'version':
        row['valuation_policy_version'] = '2'
    else:
        row['known_at_status'] = 'SIGNAL_TIME_STATE_KNOWN'
    with pytest.raises(DataQualityError):
        load()


def test_no_boundary_or_nonpositive_holding_carry(scoped):
    symbol, observation, days, resume, _, _, load = scoped
    evidence = load()
    for day in (observation['date'], resume):
        with pytest.raises(DataQualityError):
            evidence.carry(symbol, day, shares=100, raw_close=None, previous_observation=observation)
    for shares in (0, -1, True):
        with pytest.raises(DataQualityError):
            evidence.carry(symbol, days[0], shares=shares, raw_close=None, previous_observation=observation)
    with pytest.raises(DataQualityError):
        evidence.carry(symbol, days[0], shares=100, raw_close=10.25, previous_observation=observation)
