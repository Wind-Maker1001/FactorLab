"""Demand classification is not source qualification or a row-deletion policy."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from factorlab.data.hub.price_consumption import PriceConsumptionError
from tests.research.test_p10_consumer_admission import qualified, reviewed_source, replay

spec = importlib.util.spec_from_file_location('p10_g04', Path(__file__).resolve().parents[2] / 'tools/research/run_p10_g04.py')
g04 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g04)


def test_demand_preserves_suspended_missing_and_zero_volume_rows():
    bars = pd.DataFrame(dict(date=pd.to_datetime(['2023-04-24', '2023-04-25', '2023-04-26', '2023-04-27']),
        symbol=['002408.SZ'] * 4, raw_open=[10., None, 10., float('inf')],
        raw_high=[11., None, 11., 11.], raw_low=[9., None, 9., 9.],
        raw_close=[10., None, 10., 10.], volume=[100, 0, 0, 100], suspended=[0, 1, 0, None]))
    original = bars.copy(deep=True)
    result = g04.price_demand(bars)
    assert result['rows'] == 4
    assert result['positive_volume_complete_ohlc_not_suspended_candidates'] == 1
    assert result['suspended_rows'] == 1
    assert result['missing_ohlc_rows'] == 2
    assert result['unknown_suspension_rows'] == 1
    pd.testing.assert_frame_equal(bars, original)


def test_no_economic_contract_even_for_finite_prices():
    result = qualified()
    for field in result.metadata.field_semantics:
        result.metadata.field_semantics[field].pop('consumption')
    with pytest.raises(PriceConsumptionError, match='CONTRACT_MISSING_OR_UNKNOWN'):
        replay.qualified_replay_prices(result, identity=('fixture', 'version'))


def test_g03_scope_does_not_drop_suspended_observation(reviewed_source):
    bars, manifest, shard, identity = reviewed_source
    state = bars.assign(date=pd.Timestamp('2020-01-03'), raw_open=None,
                        raw_high=None, raw_low=None, raw_close=None, volume=0, suspended=1)
    all_rows = pd.concat([bars.assign(suspended=0), state], ignore_index=True)
    source = replay.replay_source_result(all_rows, manifest, source_identities=[shard])
    with pytest.raises(PriceConsumptionError, match='OBSERVATION_SCOPE_MISSING_OR_AMBIGUOUS'):
        replay.qualified_replay_prices(source, identity=identity)
    pd.testing.assert_frame_equal(source.data, all_rows)


def test_g03_field_identity_and_other_supplier_rejected(reviewed_source):
    bars, manifest, shard, identity = reviewed_source
    result = replay.replay_source_result(bars, manifest, source_identities=[shard])
    result.metadata.field_semantics['raw_open']['consumption']['field'] = 'adjusted_open'
    with pytest.raises(PriceConsumptionError, match='FIELD_IDENTITY_MISMATCH'):
        replay.qualified_replay_prices(result, identity=identity)
    with pytest.raises(PriceConsumptionError, match='SOURCE_VERSION_IDENTITY'):
        replay.qualified_replay_prices(result, identity=('OtherVendor/daily', identity[1]))
