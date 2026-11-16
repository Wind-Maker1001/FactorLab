import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.experiments import mc_grid_tail_convergence as study
from tests.research.test_mc_grid_tail_convergence import settings


def harness(tmp_path, monkeypatch, *, budget=1.1, models=('gbm', 'moving_block_bootstrap')):
    definition, config, rules = settings()
    row = study.account_metrics(np.full(5, config.initial_price), start=definition['start'],
        config=config, rules=rules, capacity='0.10', output=tmp_path/'reference')
    definition['models'] = list(models)
    definition['experiment'] |= dict(admission_unit='model_all_seeds', sizes=[8, 16],
        steps=4, resamples=9, budget_seconds=budget)
    clock = [0.]
    calls = []
    monkeypatch.setattr(study, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(study, 'scenario_models', lambda *a, **kw: ({}, None))
    def execute(prices, **kw):
        clock[0] += .01
        kw['output'].mkdir()
        return row
    def batches(model, inputs, generator, **kw):
        calls.append((model, kw['seed'], kw['begin'], kw['end']))
        yield kw['begin'], np.full((kw['end']-kw['begin'], 5), config.initial_price), {}
    monkeypatch.setattr(study, 'account_metrics', execute)
    monkeypatch.setattr(study, 'batches', batches)
    def run():
        return study.run_study(tmp_path/'run', inputs=SimpleNamespace(initial_prices=[config.initial_price],
            assets=['NVDA']), receipt=dict(as_of=definition['as_of']), config=config,
            rules=rules, definition=definition)
    return run, definition, clock, calls


def test_single_model_groups_and_incremental_prefix(tmp_path, monkeypatch):
    run, definition, clock, calls = harness(tmp_path, monkeypatch, budget=2)
    result = run()
    assert result['completed_paths_per_seed_by_model'] == {'gbm': 16, 'moving_block_bootstrap': 16}
    assert result['decisions'][0]['additional_accounts'] == 24
    assert calls[:3] == [('gbm', seed, 0, 8) for seed in definition['experiment']['seeds']]
    assert calls[3:6] == [('gbm', seed, 8, 16) for seed in definition['experiment']['seeds']]
    paths = pd.read_csv(tmp_path/'run/paths.csv')
    assert not paths.duplicated(['model', 'seed', 'path_id']).any()
    assert len(paths) == 96


def test_rejected_extension_preserves_group_and_attempts_next_model(tmp_path, monkeypatch):
    run, definition, clock, calls = harness(tmp_path, monkeypatch, budget=.5)
    result = run()
    assert result['completed_paths_per_seed_by_model']['gbm'] == 8
    assert result['decisions'][1]['model'] == 'gbm'
    assert not result['decisions'][1]['admitted']
    assert result['decisions'][2]['model'] == 'moving_block_bootstrap'
    cells = json.loads((tmp_path/'run/groups.json').read_text())
    assert len(cells) == 3
    assert {c['seed'] for c in cells} == set(definition['experiment']['seeds'])
    assert len(pd.read_csv(tmp_path/'run/paths.csv')) == 24


def test_partial_seed_group_never_publishes_formal_statistics(tmp_path, monkeypatch):
    run, definition, clock, calls = harness(tmp_path, monkeypatch, budget=2)
    original = study.grouped_statistics
    count = [0]
    def interrupted(*a, **kw):
        count[0] += 1
        if count[0] == 2:
            raise RuntimeError('simulated interruption')
        return original(*a, **kw)
    monkeypatch.setattr(study, 'grouped_statistics', interrupted)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        run()
    assert not (tmp_path/'run/groups.json').exists()
    assert len(pd.read_csv(tmp_path/'run/paths.csv')) == 16
    cells = [dict(model='gbm', seed=1729, n_paths=8,
        groups={g: dict(n=0) for g in study.GROUPS})]
    assert all(item['status'] == 'INSUFFICIENT_DATA'
        for item in study.seed_dispersion(cells, [1729, 2718, 31415]))


def test_simulation_cannot_start_before_calibration(tmp_path, monkeypatch):
    run, definition, clock, calls = harness(tmp_path, monkeypatch)
    definition['start'] = definition['as_of']
    with pytest.raises(ValueError, match='simulation start'):
        run()
    assert not calls
