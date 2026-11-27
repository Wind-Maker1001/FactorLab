from pathlib import Path

import numpy as np
import pytest

from factorlab.experiments import mc05_f06 as study
from factorlab.research.grid_simulation import BatchGridResult, COLUMNS, UnsupportedGridSimulation
from factorlab.research.monte_carlo import MonteCarloInput

ROOT = Path(__file__).resolve().parents[2]


def definition():
    return study.load_definition(ROOT/'configs/research/mc05_f06.json', ROOT)


def test_stream_allocation_at_million_scale():
    config = definition()
    seeds = [study.stream_seed(config, model, seed, steps, chunk)
        for model in config['models'] for seed in config['seeds']
        for steps in config['horizons'] for chunk in range(1000)]
    assert len(set(seeds)) == len(seeds)
    with pytest.raises(ValueError, match='allocation'):
        study.stream_seed(config, 'gbm', 1729, 168, 2**20)


@pytest.mark.parametrize('model', ['gbm', 'moving_block_bootstrap'])
def test_fixed_chunk_regeneration_and_prefix(model):
    config = definition()
    config['generation_chunk_size'] = 7
    history = np.tile([-.01, .02, -.005, .003], 20)[:, None]
    dates = np.datetime64('2020-01-01') + np.arange(len(history)).astype('timedelta64[h]')
    inputs = MonteCarloInput(weights=[1.], drift=[0.], volatility=[.01],
        initial_prices=[204.34], historical_returns=history, timestamps=dates,
        available_at=dates, as_of=str(dates[-1]))
    a, receipt = study.generate_chunk(config, inputs, {}, model=model, seed=1729, steps=168, chunk=0)
    config['execution_batch_size'] = 1
    b, repeated = study.generate_chunk(config, inputs, {}, model=model, seed=1729, steps=168, chunk=0)
    np.testing.assert_array_equal(a[:3], b[:3])
    assert receipt['effective_seed'] == repeated['effective_seed']
    assert receipt['frozen_price_digest'] == repeated['frozen_price_digest']
    assert receipt['original_price_digest'] == repeated['original_price_digest']
    c, _ = study.generate_chunk(config, inputs, {}, model=model, seed=2718, steps=168, chunk=0)
    assert not np.array_equal(a, c)


def test_unsupported_path_is_retained_and_paper_resolved(tmp_path, monkeypatch):
    config = study.batch_configuration(definition())
    prices = np.array([[204.34, 200.], [204.34, 180.]])
    terminal = np.zeros((2, len(COLUMNS)))
    terminal[:, COLUMNS.index('equity')] = [1000, 998]
    calls = []
    def simulate(values, config, *, backend, **kwargs):
        calls.append((len(values), backend))
        if backend != 'paper' and (len(values) > 1 or values[0, -1] == 180):
            raise UnsupportedGridSimulation('sensitive monetary boundary')
        row = 0 if values[0, -1] == 200 else 1
        return BatchGridResult(terminal[row:row+1], None, {})
    monkeypatch.setattr(study, 'simulate', simulate)
    values, exceptions = study.execute(prices, config, backend='cpu', output=tmp_path,
        first_path_id=100, execution_batch_size=2)
    np.testing.assert_array_equal(values, terminal)
    assert exceptions[0]['path_id'] == 101 and exceptions[0]['resolved']
    np.testing.assert_array_equal(np.load(tmp_path/'unsupported/path-101.npy'), prices[1])
    assert calls[-1] == (1, 'paper')


@pytest.mark.parametrize('selection', [{'models': []}, {'seeds': []}, {'sizes': []},
    {'models': ['gbm', 'gbm']}, {'seeds': [1729, 1729]}, {'invalid_start': True}])
def test_invalid_requests_do_not_create_success_output(tmp_path, monkeypatch, selection):
    from types import SimpleNamespace
    config = definition()
    monkeypatch.setattr(study, 'historical_input', lambda *args:
        (SimpleNamespace(assets=['BINANCE:USDM:NVDAUSDT']), config['dataset']))
    monkeypatch.setattr(study, 'scenario_models', lambda *args, **kwargs: ({}, None))
    request = selection.copy()
    if request.pop('invalid_start', False):
        config['start'] = '2026-11-13T00:00:00Z'
    with pytest.raises(ValueError):
        study.run_study(tmp_path/'invalid', definition=config, snapshot='unused', root=ROOT, **request)
    assert not (tmp_path/'invalid').exists()


def test_auxiliary_zero_inventory_and_terminal_exposure():
    terminal = np.zeros((2, len(COLUMNS)))
    terminal[1, COLUMNS.index('fills')] = 2
    terminal[1, COLUMNS.index('inventory')] = .1
    aux = study.auxiliary(np.array([[204.34]*3, [204.34, 190., 200.]]), terminal)
    assert aux['ever_inventory'].tolist() == [False, True]
    assert aux['terminal_exposure'].tolist() == [0., 20.]
    assert aux['efficiency_ratio'][0] == 0
