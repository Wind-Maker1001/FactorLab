"""Input contracts independent of compiled CPU/CUDA execution."""
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest

from factorlab.experiments.grid_funding_adaptive import FundingPolicy, VolatilityPolicy
from factorlab.research.grid_simulation import BatchGridConfig, BatchMarketInputs, UnsupportedGridSimulation
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules

HOUR = 3_600_000_000


def config(**kwargs):
    grid = GridConfig(mode='arithmetic', lower=90., upper=110., levels=1,
                      initial_price=100., quantity=1, initial_inventory=0, max_inventory=2)
    rules = GridRules(quantity_unit=Decimal('.01'), step_size=Decimal('.01'),
                      tick_size=Decimal('.01'), min_quantity=Decimal('.01'),
                      min_notional=Decimal('0'))
    return BatchGridConfig(grid, rules, **kwargs)


def market(**kwargs):
    return BatchMarketInputs(observation_times=np.asarray([HOUR-1, 2*HOUR-1, 3*HOUR-1]), **kwargs)


def test_funding_broadcast_exact_signed_coefficients_and_history():
    cfg = config(funding_policy=FundingPolicy())
    times = np.asarray([-HOUR-1, -1, 2*HOUR-1])
    data = market(funding_times=times, expected_funding_times=times,
                  funding_rates=[Decimal('.000000005'), Decimal('0'), Decimal('-.0003')],
                  funding_marks=[Decimal('101.1234567'), Decimal('100'), Decimal('99.87654321')])
    prepared = data.prepare(np.full((2, 3), 100.), cfg)
    assert prepared.funding_rates.shape == (2, 3)
    assert np.array_equal(prepared.funding_us, times)
    expected = -Fraction('.01') * Fraction('101.1234567') * Fraction('.000000005') * 100_000_000
    assert tuple(prepared.funding_coeff[0, 0]) == (expected.numerator, expected.denominator)
    assert tuple(prepared.funding_coeff[1, 1]) == (0, 1)
    assert prepared.funding_coeff[0, 2, 0] > 0


@pytest.mark.parametrize('times', [[HOUR-1, HOUR-1, 3*HOUR-1], [HOUR-1, 3*HOUR-1, 2*HOUR-1],
                                  [HOUR-1, 3*HOUR-1, 4*HOUR-1], [0, HOUR, 2*HOUR]])
def test_b_rejects_duplicate_missing_unordered_or_open_timestamps(times):
    with pytest.raises(ValueError):
        BatchMarketInputs(times).prepare(np.full((1, 3), 100.), config(funding_policy=FundingPolicy()))


def test_expected_schedule_distinguishes_missing_from_zero():
    data = market(funding_times=[2*HOUR-1], expected_funding_times=[2*HOUR-1, 3*HOUR-1],
                  funding_rates=[0], funding_marks=[100])
    with pytest.raises(ValueError, match='missing settlements'):
        data.prepare(np.full((1, 3), 100.), config())


@pytest.mark.parametrize('changes', [dict(funding_rates=None), dict(funding_times=[HOUR-1, HOUR-1]),
    dict(funding_marks=[float('nan')]), dict(funding_rates=[[0], [0], [0]]),
    dict(funding_times=[4*HOUR-1], expected_funding_times=[4*HOUR-1]),
    dict(funding_rates=[Decimal('0.00000000000000000000000000000000001')])])
def test_invalid_funding_inputs(changes):
    fields = dict(funding_times=[2*HOUR-1], expected_funding_times=[2*HOUR-1],
                  funding_rates=[0], funding_marks=[100])
    fields.update(changes)
    with pytest.raises(ValueError):
        market(**fields).prepare(np.full((2, 3), 100.), config())


def test_atr_requires_real_high_low_and_rejects_inconsistent_bars():
    cfg = config()
    cfg = replace(cfg, grid=replace(cfg.grid, mode='atr'))
    with pytest.raises(ValueError, match='actual completed'):
        market().prepare(np.full((1, 3), 100.), cfg)
    with pytest.raises(ValueError, match='low <= close <= high'):
        market(highs=[[99, 101, 101]], lows=[[98, 99, 99]]).prepare(np.full((1, 3), 100.), cfg)
    prepared = market(highs=[[102, 103, 104]], lows=[[98, 99, 100]]).prepare(np.full((1, 3), 100.), cfg)
    assert prepared.bars.shape == (1, 3, 2)


def test_c_warmup_includes_anchor_and_rejects_future_or_misaligned_data():
    cfg = config(volatility_policy=VolatilityPolicy(vol_window_hours=2))
    with pytest.raises(ValueError, match='explicit warmup'):
        market().prepare(np.full((1, 3), 100.), cfg)
    with pytest.raises(ValueError, match='ending at anchor'):
        market(warmup_closes=[98, 99, 101]).prepare(np.full((1, 3), 100.), cfg)
    prepared = market(warmup_closes=[98, 99, 100]).prepare(np.full((2, 3), 100.), cfg)
    assert prepared.warmup.shape == (2, 3)
    assert prepared.warmup.flags.c_contiguous


def test_profiles_reuse_policy_types_and_reject_unsupported_combinations():
    with pytest.raises(UnsupportedGridSimulation, match='cannot combine'):
        config(funding_policy=FundingPolicy(), volatility_policy=VolatilityPolicy())
    cfg = config(volatility_policy=VolatilityPolicy())
    with pytest.raises(UnsupportedGridSimulation, match='one level'):
        replace(cfg, grid=replace(cfg.grid, levels=2))
    with pytest.raises(UnsupportedGridSimulation, match='cannot combine'):
        replace(cfg, grid=replace(cfg.grid, mode='atr'))
    targets, limits, params, integers = cfg.packed(3)
    assert params.shape == (28,)
    assert integers.shape == (23,)
    assert integers[11] == 1
    assert cfg.workspace_width == 49


def test_real_whole_hour_settlement_keeps_timestamp_and_decimal_precision():
    rate, mark = Decimal('0.000000005'), Decimal('101.1234567')
    prepared = market(funding_times=[HOUR], expected_funding_times=[HOUR],
                      funding_rates=[rate], funding_marks=[mark]).prepare(np.full((2, 3), 100.), config())
    assert prepared.funding_us[0] == HOUR
    assert prepared.observation_us[0] < HOUR < prepared.observation_us[1]
    assert prepared.funding_rates_decimal[1, 0] == rate
    assert prepared.funding_marks_decimal[1, 0] == mark


def test_anchor_settlement_rejected_without_pre_endowment_holding_convention():
    with pytest.raises(UnsupportedGridSimulation, match='anchor settlement'):
        market(funding_times=[HOUR-1], expected_funding_times=[HOUR-1],
               funding_rates=[0], funding_marks=[100]).prepare(np.full((1, 3), 100.), config())


def test_naive_datetime_rejected_and_aware_utc_preserves_microseconds():
    with pytest.raises(ValueError, match='timezone-aware'):
        BatchMarketInputs(['2026-01-01T00:59:59.999999']).prepare(np.full((1, 1), 100.), config())
    prepared = BatchMarketInputs(['1970-01-01T00:59:59.999999Z']).prepare(np.full((1, 1), 100.), config())
    assert prepared.observation_us[0] == HOUR-1
