from decimal import Decimal as D
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments import grid_parameter_validation as validation
from factorlab.research.grid_simulation import BatchGridConfig
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


@pytest.fixture
def config():
    return BatchGridConfig(GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
        initial_inventory=0, initial_price=100, quantity=10, max_inventory=100,
        max_cash_committed=1000, breakout='pause', order_ttl=100,
        fee_bps=4, slippage_bps=6), GridRules(D('.1'), D('.1'), D('.01'), D('.1'), D('5')),
        capacity=D('1'))


def sample(monkeypatch):
    times = pd.date_range('2026-07-15', periods=8, freq='h', tz='UTC')
    bars = [dict(time=t.isoformat(), open=200 + i, close=201 + i) for i, t in enumerate(times)]
    bars.insert(0, dict(time=(times[0] - pd.Timedelta(hours=1)).isoformat(), open=199, close=200))
    monkeypatch.setattr(validation, 'load_hourly', lambda _: (bars, dict(
        bars_sha256=validation.CERTIFIED_BARS_SHA256)))
    return bars


def test_freeze_chronology_anchor_and_no_lookahead(monkeypatch):
    bars = sample(monkeypatch)
    manifest = validation.freeze_windows('unused', end='2026-07-15T08:00:00Z', steps=3)
    windows = manifest['windows']
    assert [w['steps'] for w in windows] == [3, 3, 2]
    assert windows[0]['prices'] == [200, 201, 202, 203]
    assert windows[0]['market_state']['known_returns'] == 0
    assert windows[1]['market_state']['known_returns'] == 3
    assert windows[0]['end'] == windows[1]['start']
    state = windows[1]['market_state']
    bars[4]['close'] = 999
    changed = validation.freeze_windows('unused', end='2026-07-15T08:00:00Z', steps=3)
    assert changed['windows'][1]['market_state'] == state
    assert changed['windows'][1]['id'] != windows[1]['id']
    assert 'not historically untouched' in manifest['provenance_warning']
    json.dumps(manifest, allow_nan=False)


def test_freeze_rejects_missing_coverage(monkeypatch):
    sample(monkeypatch)
    with pytest.raises(ValueError, match='cover'):
        validation.freeze_windows('unused', end='2026-08-13T00:00:00Z')


def test_replay_crossed_stress_aligns_all_costs(monkeypatch, config):
    sample(monkeypatch)
    manifest = validation.freeze_windows('unused', end='2026-07-15T03:00:00Z', steps=3)
    seen = []
    original = validation.simulate

    def record(prices, settings, **kwargs):
        seen.append((prices, settings))
        return original(prices, settings, **kwargs)

    monkeypatch.setattr(validation, 'simulate', record)
    result = validation.replay_windows(manifest, [('candidate', config)], backend='paper')
    assert len(result['results']) == 4
    assert [s.capacity for _, s in seen] == [D('1'), D('.5'), D('1'), D('.5')]
    for prices, settings in seen:
        assert prices[0, 0] == config.grid.initial_price
        assert settings.grid.slippage_bps / 10000 == float(settings.slippage)
        assert settings.grid.fee_bps / 10000 == float(settings.fee_rate)
    json.dumps(result, allow_nan=False)


def test_persisted_paper_fifo_matches_in_memory(tmp_path, config):
    prices = np.asarray([[100, 89, 101, 89, 111, 89, 101]], dtype=float)
    output = tmp_path / 'audit'
    result = validation.audit_paths(output, prices, config, backend='paper', path_ids=['extreme-7'])
    assert result['results'][0]['matched']
    assert (output / 'path-00000' / 'paper.sqlite').is_file()
    assert (output / 'path-00000' / 'fifo.json').is_file()
    assert json.loads((output / 'inputs.json').read_text())['path_ids'] == ['extreme-7']
    assert np.load(output / 'prices.npy').tolist() == prices.tolist()


def test_audit_fails_on_execution_trace_mismatch(tmp_path, monkeypatch, config):
    original = validation.simulate

    def divergent(prices, settings, *, backend, trace):
        result = original(prices, settings, backend='paper', trace=trace)
        if backend == 'cpu':
            result.trace[0, -1, 0] += 1
        return result

    monkeypatch.setattr(validation, 'simulate', divergent)
    with pytest.raises(AssertionError):
        validation.audit_paths(tmp_path / 'audit', [[100, 89, 101]], config, backend='cpu')
    assert (tmp_path / 'audit' / 'path-00000' / 'comparison.npz').is_file()


def test_audit_rejects_unfrozen_quotes(tmp_path, config):
    with pytest.raises(ValueError, match='six-decimal'):
        validation.audit_paths(tmp_path / 'audit', [[100, 89.1234567]], config, backend='cpu')
