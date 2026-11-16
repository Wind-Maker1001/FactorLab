"""Frozen-state, joint-block, jump and temporal information contracts."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from factorlab.research.regime_jump import (
    CalibrationError, JumpConfig, JumpStress, PathPlan, RegimeBootstrap, RegimeConfig,
)


@pytest.fixture
def history():
    rng = np.random.default_rng(92)
    logs = rng.normal(0, .01, 800)
    returns = np.column_stack([np.expm1(logs), np.expm1(2 * logs)])
    times = pd.date_range('2025-01-01', periods=800, freq='h', tz='UTC')
    return dict(returns=returns, timestamps=times, available_at=times,
        as_of=times[-1], threshold_as_of=times[299], assets=('A', 'B'),
        initial_prices=[100., 50.], source=dict(source='SYNTHETIC_TEST', data_version='seed92'),
        config=RegimeConfig(reference_asset='A', block_size=5, lookback=6,
            min_blocks_per_state=2, min_transitions_per_state=2))


def collect(model, plan):
    return np.concatenate([paths for _, paths in model.batches(plan)])


def test_seed_and_batch_layout_are_reproducible(history):
    model = RegimeBootstrap(**history)
    plan = PathPlan(n_paths=7, n_steps=19, batch_size=3, seed=123)
    a = collect(model, plan)
    np.testing.assert_array_equal(a, collect(model, plan))
    np.testing.assert_array_equal(a, collect(model, replace(plan, batch_size=1)))
    assert a.shape == (7, 20, 2)
    assert np.isfinite(a).all() and (a > 0).all()
    assert not np.array_equal(a, collect(model, replace(plan, seed=124)))
    np.testing.assert_allclose(np.log(a[:, :, 1] / 50), 2 * np.log(a[:, :, 0] / 100), atol=2e-15)


def test_block_order_and_common_asset_indices(history):
    model = RegimeBootstrap(**history, transition=np.eye(4), initial_state=[1, 0, 0, 0])
    paths = collect(model, PathPlan(n_paths=4, n_steps=17, batch_size=2))
    logs = np.log1p(history['returns'])
    for path in paths:
        sampled = np.diff(np.log(path), axis=0)
        for begin in range(0, 17, 5):
            block = sampled[begin:begin + 5]
            assert any(np.allclose(block, logs[s:s + len(block)], rtol=0, atol=2e-15)
                       for s in model._pools[0])


def test_prefix_threshold_lagged_features_and_block_lag_counts(history):
    model = RegimeBootstrap(**history)
    logs = np.log1p(history['returns'][:, 0])
    vol = [np.std(logs[s - 6:s], ddof=1) for s in range(6, 301)]
    threshold = float(np.quantile(vol, .5))
    receipt = model.receipt()
    assert receipt['calibration']['volatility_threshold'] == pytest.approx(threshold)
    assert receipt['calibration']['threshold_available_at'] == history['timestamps'][299].isoformat()
    labels = {}
    for s in range(300, 796):
        preceding = logs[s - 6:s]
        state = 2 * (np.std(preceding, ddof=1) > threshold) + (preceding.sum() >= 0)
        labels[s] = int(state)
        assert s in model._pools[state]
    expected = np.zeros((4, 4), int)
    for s in range(300, 791):
        expected[labels[s], labels[s + 5]] += 1
    np.testing.assert_array_equal(receipt['calibration']['transition_counts'], expected)
    # Changing a block's first return cannot alter its own lagged state.
    changed = history['returns'].copy()
    changed[500, 0] = .4
    second = RegimeBootstrap(**dict(history, returns=changed))
    assert next(i for i, pool in enumerate(model._pools) if 500 in pool) == next(
        i for i, pool in enumerate(second._pools) if 500 in pool)
    assert second.receipt()['calibration']['volatility_threshold'] == threshold


def test_inputs_are_snapshotted_and_receipts_are_compact(history):
    model = RegimeBootstrap(**history)
    before = collect(model, PathPlan(n_paths=1, n_steps=10))
    receipt = model.receipt()
    history['returns'][:] = 0
    history['source']['data_version'] = 'changed'
    np.testing.assert_array_equal(before, collect(model, PathPlan(n_paths=1, n_steps=10)))
    assert model.receipt() == receipt
    assert 'returns' not in receipt['input']
    assert receipt['input']['source']['data_version'] == 'seed92'


@pytest.mark.parametrize('change', [
    dict(returns=np.full((800, 2), np.nan)), dict(returns=np.full((800, 2), -1.)),
    dict(returns=np.zeros((800, 1))), dict(initial_prices=[100, 0]),
    dict(assets=('A', 'A')), dict(source={}), dict(available_at=None),
    dict(as_of='2025-01-01T00:00:00Z'), dict(as_of='2025-03-01'),
    dict(threshold_as_of='2026-01-01T00:00:00Z'),
])
def test_invalid_or_incomplete_history_rejected(history, change):
    with pytest.raises((ValueError, TypeError)):
        RegimeBootstrap(**dict(history, **change))


@pytest.mark.parametrize('kind', ['duplicate', 'gap', 'future_availability', 'early_availability', 'nat'])
def test_temporal_boundaries_rejected(history, kind):
    times, available = list(history['timestamps']), list(history['available_at'])
    if kind == 'duplicate':
        times[50] = times[49]
    elif kind == 'gap':
        times[50] += pd.Timedelta(minutes=1)
    elif kind == 'future_availability':
        available[0] = history['as_of'] + pd.Timedelta(hours=1)
    elif kind == 'early_availability':
        available[0] -= pd.Timedelta(hours=1)
    else:
        available[0] = pd.NaT
    with pytest.raises(ValueError):
        RegimeBootstrap(**dict(history, timestamps=times, available_at=available))


def test_unavailable_tail_is_rejected_not_removed(history):
    available = list(history['available_at'])
    available[-1] += pd.Timedelta(hours=1)
    with pytest.raises(ValueError, match='no later'):
        RegimeBootstrap(**dict(history, available_at=available))


def test_support_rejection_and_explicit_transition_stress(history):
    cfg = replace(history['config'], min_transitions_per_state=10_000)
    with pytest.raises(CalibrationError, match='transition support'):
        RegimeBootstrap(**dict(history, config=cfg))
    model = RegimeBootstrap(**dict(history, config=cfg), transition=np.eye(4))
    assert 'PARAMETERIZED' in model.receipt()['parameters']['transition_source']
    with pytest.raises(CalibrationError, match='block support'):
        RegimeBootstrap(**dict(history, config=replace(cfg, min_blocks_per_state=10_000)), transition=np.eye(4))


@pytest.mark.parametrize('matrix', [np.zeros((4, 4)), np.ones((3, 3)), np.full((4, 4), np.nan),
                                    np.tile([-.1, .1, .5, .5], (4, 1))])
def test_invalid_transitions_rejected(history, matrix):
    with pytest.raises(ValueError, match='transition'):
        RegimeBootstrap(**history, transition=matrix)


def jump(config=JumpConfig(), assets=('A',), initial=(100.,)):
    return JumpStress(assets=assets, initial_prices=initial, as_of='2025-01-01T00:00:00Z',
        source=dict(source='PARAMETERIZED_TEST', data_version='v1'), config=config)


def test_jump_point_mass_sign_and_common_event():
    config = JumpConfig(log_drift=(0., 0.), volatility=(0., 0.), jump_probability=.4,
        jump_log_sizes=(-.2, .3))
    model = jump(config, ('A', 'B'), (100., 50.))
    plan = PathPlan(n_paths=5, n_steps=30, batch_size=2)
    paths = collect(model, plan)
    changes = np.diff(np.log(paths), axis=1)
    events = changes[:, :, 0] < -.1
    assert events.any() and (~events).any()
    np.testing.assert_allclose(changes[:, :, 0], events * -.2, atol=2e-15)
    np.testing.assert_allclose(changes[:, :, 1], events * .3, atol=2e-15)
    np.testing.assert_array_equal(paths, collect(model, replace(plan, batch_size=5)))
    assert model.receipt()['calibration']['market_probability_validated'] is False


@pytest.mark.parametrize('probability', [0., 1.])
def test_jump_probability_boundaries(probability):
    model = jump(JumpConfig(log_drift=(.01,), volatility=(0.,), jump_probability=probability, jump_log_sizes=(-.2,)))
    path = collect(model, PathPlan(n_paths=1, n_steps=3))[0, :, 0]
    np.testing.assert_allclose(path, 100 * np.exp(np.arange(4) * (.01 - probability * .2)))


@pytest.mark.parametrize('kwargs', [dict(jump_probability=-.1), dict(jump_probability=np.nan),
    dict(jump_probability=1.1), dict(volatility=(-.1,)), dict(log_drift=(np.inf,)),
    dict(jump_log_sizes=(0., 1.))])
def test_invalid_jump_parameters(kwargs):
    with pytest.raises(ValueError):
        JumpConfig(**kwargs)


@pytest.mark.parametrize('drift', [1000., -1000.])
def test_unrepresentable_prices_fail_without_clipping(drift):
    with pytest.raises(ValueError, match='float64'):
        collect(jump(JumpConfig(log_drift=(drift,), volatility=(0.,), jump_probability=0)),
            PathPlan(n_paths=1, n_steps=3))


@pytest.mark.parametrize('kwargs', [dict(n_paths=0), dict(n_steps=True), dict(batch_size=257),
    dict(seed=-1), dict(n_paths=100_001), dict(max_batch_bytes=0)])
def test_invalid_plan(kwargs):
    with pytest.raises(ValueError):
        PathPlan(**kwargs)


def test_memory_limit_before_allocation_and_batch_sizes():
    model = jump()
    with pytest.raises(ValueError, match='workspace'):
        next(model.batches(PathPlan(max_batch_bytes=1)))
    batches = list(model.batches(PathPlan(n_paths=7, n_steps=3, batch_size=3)))
    assert [begin for begin, _ in batches] == [0, 3, 6]
    assert [len(paths) for _, paths in batches] == [3, 3, 1]


@pytest.mark.parametrize('initial', [[0, 0, 0, 0], [1, 0, 0], [-1, 1, 1, 0], [np.nan, 0, 0, 1]])
def test_invalid_initial_distribution(history, initial):
    with pytest.raises(ValueError, match='initial_state'):
        RegimeBootstrap(**history, initial_state=initial)


@pytest.mark.parametrize('kwargs', [dict(block_size=0), dict(lookback=1), dict(volatility_quantile=np.nan),
    dict(min_blocks_per_state=0), dict(min_transitions_per_state=True)])
def test_invalid_regime_config(kwargs):
    with pytest.raises(ValueError):
        RegimeConfig(**kwargs)


def test_sample_identity_includes_returns_and_pit(history):
    original = RegimeBootstrap(**history).receipt()['input']['sample_hash']
    changed = history['returns'].copy()
    changed[0, 1] += .01
    assert RegimeBootstrap(**dict(history, returns=changed)).receipt()['input']['sample_hash'] != original
    available = history['available_at'] + pd.Timedelta(minutes=1)
    changed_time = dict(history, available_at=available, as_of=history['as_of'] + pd.Timedelta(hours=1))
    assert RegimeBootstrap(**changed_time).receipt()['input']['sample_hash'] != original
