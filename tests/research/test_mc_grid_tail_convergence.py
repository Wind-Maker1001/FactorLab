import json
from pathlib import Path

import numpy as np
import pandas as pd

from types import SimpleNamespace

from factorlab.experiments import mc_grid_tail_convergence as study
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import account_metrics, grouped_statistics, batches
from factorlab.research.monte_carlo.reliability import summarize_paths
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules

ROOT = Path(__file__).resolve().parents[2]


def settings():
    definition = json.loads((ROOT/'configs/mc05_grid_tail.json').read_text())
    return definition, GridConfig(**definition['grid']), GridRules(**{k:D(v) for k,v in definition['rules'].items()})


def test_zero_fill_and_reproducibility(tmp_path):
    definition, config, rules = settings()
    metrics = [account_metrics(np.full(25, config.initial_price), start=definition['start'],
        config=config, rules=rules, capacity='0.10', output=tmp_path/str(i)) for i in range(2)]
    assert metrics[0] == metrics[1]
    row = metrics[0]
    assert row['participation_class'] == 'zero_fills'
    assert row['terminal_nav'] == 1000
    result = grouped_statistics([row], resamples=19)
    assert result['traded']['status'] == 'INSUFFICIENT_DATA'
    assert result['unconditional']['statistics']['tails']['0.95']['statistical_status'] == 'STATISTICALLY_UNSTABLE'


def test_conditioned_tail_uses_mc02_definition(tmp_path):
    definition, config, rules = settings()
    row = account_metrics(np.linspace(config.initial_price, config.initial_price*.6, 25),
        start=definition['start'], config=config, rules=rules, capacity='0.10', output=tmp_path/'down')
    assert row['traded'] and row['has_terminal_inventory']
    assert abs(row['reconciliation_residual']) < 1e-8
    flat = row | dict(terminal_nav=1000, traded=False, ever_inventory=False,
        has_terminal_inventory=False, participation_class='zero_fills')
    groups = grouped_statistics([row, flat], resamples=19)
    assert groups['traded']['population_fraction']['estimate'] == .5
    expected = summarize_paths([1-row['terminal_nav']/1000], [row['maximum_drawdown']], resamples=19)
    assert groups['traded']['statistics'] == expected


def test_budget_preserves_all_seeds_and_excludes_deterministic(tmp_path, monkeypatch):
    definition, config, rules = settings()
    definition['experiment'] |= dict(sizes=[8, 16], steps=24, resamples=9, budget_seconds=1)
    clock = [0.]
    monkeypatch.setattr(study, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(study, 'scenario_models', lambda *a, **kw: ({'regime_historical': object(), 'jump_down': object()}, None))
    def execute(prices, **kw):
        clock[0] += .01
        kw['output'].mkdir()
        return dict(terminal_nav=1000)
    monkeypatch.setattr(study, 'account_metrics', execute)
    result = study.run_study(tmp_path/'run', inputs=SimpleNamespace(initial_prices=[config.initial_price], assets=['NVDA']),
        receipt=dict(as_of=definition['as_of']), config=config, rules=rules, definition=definition)
    assert len(result['deterministic']) == 6
    assert result['completed_paths_per_seed'] == 0
    assert not result['decisions'][0]['admitted']
    assert result['decisions'][0]['additional_accounts'] == 8*4*3
    assert result['statistical_status'] == 'INSUFFICIENT_DATA'
    assert pd.read_csv(tmp_path/'run'/'paths.csv').empty
    assert json.loads((tmp_path/'run'/'groups.json').read_text()) == []


def test_complete_seed_group_statistics(tmp_path, monkeypatch):
    definition, config, rules = settings()
    row = account_metrics(np.full(5, config.initial_price), start=definition['start'],
        config=config, rules=rules, capacity='0.10', output=tmp_path/'reference')
    definition['experiment'] |= dict(sizes=[8], steps=4, resamples=9, budget_seconds=900)
    monkeypatch.setattr(study, 'scenario_models', lambda *a, **kw: ({'regime_historical': object(), 'jump_down': object()}, None))
    def execute(prices, **kw):
        kw['output'].mkdir()
        return row
    monkeypatch.setattr(study, 'account_metrics', execute)
    monkeypatch.setattr(study, 'batches', lambda *a, **kw: iter([(0, np.full((8, 5), config.initial_price), {})]))
    result = study.run_study(tmp_path/'run', inputs=SimpleNamespace(initial_prices=[config.initial_price], assets=['NVDA']),
        receipt=dict(as_of=definition['as_of']), config=config, rules=rules, definition=definition)
    assert result['completed_paths_per_seed'] == 8
    paths = pd.read_csv(tmp_path/'run'/'paths.csv')
    assert len(paths) == 96
    assert paths.groupby(['model', 'seed']).size().eq(8).all()
    cells = json.loads((tmp_path/'run'/'groups.json').read_text())
    assert len(cells) == 12
    assert all(c['groups']['unconditional']['n'] == 8 for c in cells)
    assert result['statistical_status'] == 'STATISTICALLY_UNSTABLE'


def test_legacy_batch_stream_nested_prefix():
    from factorlab.research.monte_carlo import MonteCarloInput
    inputs = MonteCarloInput(weights=[1.], drift=[0.], volatility=[.01], initial_prices=[100.])
    for model in ('gbm',):
        small = list(batches(model, inputs, None, seed=1729, begin=0, end=8, batch_size=8, steps=24))
        large = list(batches(model, inputs, None, seed=1729, begin=0, end=16, batch_size=8, steps=24))
        np.testing.assert_array_equal(small[0][1], large[0][1])
        extension = list(batches(model, inputs, None, seed=1729, begin=8, end=16, batch_size=8, steps=24))
        np.testing.assert_array_equal(extension[0][1], large[1][1])
