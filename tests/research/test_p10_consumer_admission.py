"""Frozen replay prices retain source and dated identity boundaries."""
import importlib.util
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
