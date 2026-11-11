"""Frozen replay prices retain source and dated identity boundaries."""
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from factorlab.data.hub.price_consumption import PriceConsumptionError
from tests.research.test_price_consumption import envelope, IDENTITY

spec = importlib.util.spec_from_file_location('p10_replay', Path(__file__).resolve().parents[2] / 'tools/research/run_evidence_consumption.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def qualified():
    frame = pd.DataFrame(dict(date=pd.to_datetime(['2020-01-02']), symbol=['600000.SH'],
                             raw_open=[10.], raw_high=[11.], raw_low=[9.], raw_close=[10.]))
    result = envelope(frame)
    for original, field in replay.PRICE_FIELDS.items():
        result.metadata.field_semantics[field]['original_field'] = original
    return result


def test_same_qualified_prices_are_exactly_unchanged():
    result = qualified()
    actual = replay.qualified_replay_prices(result, identity=IDENTITY)
    pd.testing.assert_frame_equal(actual, result.data, check_exact=True)


def test_actual_source_manifest_does_not_grant_price_qualification():
    result = replay.replay_source_result(qualified().data, b'{}')
    with pytest.raises(PriceConsumptionError, match='CONTRACT_MISSING'):
        replay.qualified_replay_prices(result, identity=(replay.SOURCE_ID, result.metadata.data_version))


@pytest.mark.parametrize('basis', ['ADJUSTED_CANDIDATE', 'MODEL_VALUATION_ASSUMPTION', 'SUPPLIER_FILLED'])
def test_nonraw_prices_rejected(basis):
    result = qualified()
    for semantic in result.metadata.field_semantics.values():
        semantic['consumption']['basis'] = basis
    with pytest.raises(PriceConsumptionError, match='BASIS_NOT_ALLOWED'):
        replay.qualified_replay_prices(result, identity=IDENTITY)


def test_version_and_unknown_historical_availability_rejected():
    result = qualified()
    with pytest.raises(PriceConsumptionError, match='SOURCE_VERSION'):
        replay.qualified_replay_prices(result, identity=('fixture', 'wrong'))
    with pytest.raises(PriceConsumptionError, match='INFORMATION_AVAILABILITY_UNKNOWN'):
        replay.qualified_replay_prices(result, identity=IDENTITY, cutoff='2020-01-03T15:00:00+08:00')


@pytest.mark.parametrize('symbol,day', [('300114.SZ','2025-02-17'), ('302132.SZ','2025-02-14'),
                                      ('600068.SH','2021-09-13'), ('601868.SH','2021-09-27')])
def test_invalid_dated_identity_never_becomes_alias(symbol, day):
    result = qualified()
    result.data['symbol'], result.data['date'] = symbol, pd.Timestamp(day)
    with pytest.raises(PriceConsumptionError, match='DATED_SECURITY_IDENTITY'):
        replay.qualified_replay_prices(result, identity=IDENTITY)


@pytest.fixture
def reviewed_source(tmp_path, monkeypatch):
    fixture = qualified()
    bars = fixture.data.assign(volume=100)
    manifest = b'{"fixture": "frozen manifest bytes"}'
    version = replay.sha256(manifest)
    member = 'stock/cn_stock_real_bar1d/2020-01-01_2020-01-31.parquet'
    shard = dict(member=member, sha256=replay.sha256(b'fixture Parquet member'))
    fields = fixture.metadata.field_semantics
    for semantic in fields.values():
        semantic['consumption'].update(source_id=replay.SOURCE_ID, data_version=version,
                                       purposes=['EXECUTION'])
    key = '600000.SH/2020-01-02'
    observation = replay.price_observation_sha256('2020-01-02', '600000.SH',
                                                  [10., 11., 9., 10.], 100)
    evidence = dict(source_id=replay.SOURCE_ID, data_version=version, field_semantics=fields,
                    price_members={member: shard['sha256']},
                    observations={key: dict(member=member, row_sha256=observation)})
    path = tmp_path / 'source-price-contract.json'
    path.write_text(json.dumps(evidence), encoding='utf-8')
    monkeypatch.setattr(replay, 'SOURCE_PRICE_CONTRACT', path)
    return bars, manifest, shard, (replay.SOURCE_ID, version)


def test_reviewed_observation_is_admitted_without_changing_its_values(reviewed_source):
    bars, manifest, shard, identity = reviewed_source
    result = replay.replay_source_result(bars, manifest, source_identities=[shard])
    actual = replay.qualified_replay_prices(result, identity=identity)
    pd.testing.assert_frame_equal(actual, bars, check_exact=True)


@pytest.mark.parametrize('drift', ['manifest', 'member'])
def test_evidence_does_not_follow_a_different_source_version(reviewed_source, drift):
    bars, manifest, shard, identity = reviewed_source
    if drift == 'manifest':
        manifest += b' '
    else:
        shard['sha256'] = replay.sha256(b'replacement member')
    result = replay.replay_source_result(bars, manifest, source_identities=[shard])
    with pytest.raises(PriceConsumptionError, match='CONTRACT_MISSING_OR_UNKNOWN'):
        replay.qualified_replay_prices(result, identity=identity)


@pytest.mark.parametrize('changes', [
    {'raw_open': 20.},
    {'raw_open': 10., 'raw_high': 10., 'raw_low': 10., 'volume': 0},
    {'raw_close': 9.5, 'valuation_mark': 9.5},
])
def test_original_column_names_cannot_authorize_replacement_prices(reviewed_source, changes):
    bars, manifest, shard, identity = reviewed_source
    result = replay.replay_source_result(bars.assign(**changes), manifest, source_identities=[shard])
    with pytest.raises(PriceConsumptionError, match='SOURCE_PRICE_PROJECTION_DRIFT'):
        replay.qualified_replay_prices(result, identity=identity)


@pytest.mark.parametrize('change, reason', [
    ('field', 'ORIGINAL_FIELD_IDENTITY_UNKNOWN'),
    ('unit', 'UNIT_UNKNOWN_OR_MISMATCH'),
    ('cutoff', 'INFORMATION_AVAILABILITY_UNKNOWN'),
    ('security', 'OBSERVATION_SCOPE_MISSING_OR_AMBIGUOUS'),
    ('date', 'OBSERVATION_SCOPE_MISSING_OR_AMBIGUOUS'),
])
def test_scoped_evidence_does_not_extend_to_other_inputs(reviewed_source, change, reason):
    bars, manifest, shard, identity = reviewed_source
    result = replay.replay_source_result(bars, manifest, source_identities=[shard])
    semantic = result.metadata.field_semantics['raw_open']
    cutoff = None
    if change == 'field':
        semantic['original_field'] = 'adjusted_open'
    elif change == 'unit':
        semantic['consumption']['unit'] = None
    elif change == 'cutoff':
        cutoff = '2020-01-03T15:00:00+08:00'
    elif change == 'security':
        bars['symbol'] = '600001.SH'
    else:
        bars['date'] = pd.Timestamp('2020-01-03')
    with pytest.raises(PriceConsumptionError, match=reason):
        replay.qualified_replay_prices(result, identity=identity, cutoff=cutoff)
