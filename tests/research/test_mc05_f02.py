import json

import numpy as np
import pytest

from factorlab.experiments import mc_grid_tail_convergence as study
from factorlab.research.regime_jump import PathPlan
from tests.research.test_mc05_f01 import harness
from tests.research.test_mc_regime_jump import study_input
from tests.research.test_mc_grid_tail_convergence import settings


def test_coverage_first_without_repeating_deterministic_accounts(tmp_path, monkeypatch):
    run, definition, _, calls = harness(tmp_path, monkeypatch, budget=2)
    definition['experiment'].update(coverage_first=True, run_deterministic=False)
    definition['resource_planning'] = dict(models={m: dict(
        seconds_per_account_including_cleanup=.01, group_generation_statistics_seconds=0)
        for m in definition['models']})
    result = run()
    assert [(d['model'], d['size']) for d in result['decisions']] == [
        ('gbm', 8), ('moving_block_bootstrap', 8), ('gbm', 16), ('moving_block_bootstrap', 16)]
    assert result['deterministic'] == []
    assert result['performance']['accounts'] == 96
    assert calls[:3] == [('gbm', s, 0, 8) for s in definition['experiment']['seeds']]


def test_frozen_cost_rejects_complete_group_without_accounts(tmp_path, monkeypatch):
    run, definition, _, calls = harness(tmp_path, monkeypatch, budget=.1)
    definition['experiment'].update(coverage_first=True, run_deterministic=False)
    definition['resource_planning'] = dict(models={m: dict(
        seconds_per_account_including_cleanup=1, group_generation_statistics_seconds=0)
        for m in definition['models']})
    result = run()
    assert not calls
    assert all(d['status'] == 'RESOURCE_NOT_ADMITTED' for d in result['decisions'])
    assert result['statistical_status'] == 'INSUFFICIENT_DATA'


@pytest.mark.parametrize('model', ['jump_down', 'regime_historical'])
def test_latent_evidence_replays_the_same_path(model):
    inputs, receipt, threshold, config = study_input()
    models, _ = study.scenario_models(inputs, receipt, threshold_as_of=threshold, regime_config=config)
    generator = models[model]
    plan = PathPlan(seed=1729, n_paths=8, n_steps=168)
    _, paths = next(generator.batches(plan))
    for path_id in range(8):
        evidence = generator.path_evidence(plan, path_id)
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([plan.seed, path_id])))
        direct = []
        np.testing.assert_array_equal(generator._path(rng, plan.n_steps, evidence=direct), paths[path_id])
        assert direct == evidence
        if model == 'regime_historical':
            assert sum(e['size'] for e in evidence) == plan.n_steps
            assert [e['begin'] for e in evidence] == list(range(0, 168, config.block_size))
        else:
            assert all(e['jump_log_sizes'][0] == pytest.approx(np.log(.7)) for e in evidence)


@pytest.mark.parametrize('steps', [4, 24])
def test_real_account_latent_state_join_and_diagnostics(tmp_path, monkeypatch, steps):
    inputs, receipt, threshold, regime = study_input()
    definition, config, rules = settings()
    models, _ = study.scenario_models(inputs, receipt, threshold_as_of=threshold, regime_config=regime)
    monkeypatch.setattr(study, 'scenario_models', lambda *a, **kw: (models, None))
    definition.update(start='2030-01-01T00:00:00Z', models=['regime_historical'])
    definition['experiment'].update(coverage_first=True, run_deterministic=False, path_evidence=True,
        admission_unit='model_all_seeds', sizes=[1], batch_size=1, seeds=[1729], steps=steps, resamples=9, budget_seconds=100)
    definition['resource_planning'] = dict(models={'regime_historical': dict(
        seconds_per_account_including_cleanup=1, group_generation_statistics_seconds=1)})
    from dataclasses import replace
    price = float(inputs.initial_prices[0])
    config = replace(config, initial_price=price, lower=.8*price, upper=1.2*price)
    result = study.run_study(tmp_path/'run', inputs=inputs, receipt=receipt, config=config,
        rules=rules, definition=definition)
    assert result['performance']['accounts'] == 1
    evidence = json.loads((tmp_path/'run/path-evidence.json').read_text())
    assert sum(e['size'] for e in evidence[0]['events']) == steps
    assert all('traded_hours' in e for e in evidence[0]['events'])
    assert result['model_execution_status'] == {'regime_historical': 'COMPLETED'}


def test_no_deterministic_requires_frozen_cost_admission(tmp_path, monkeypatch):
    run, definition, _, calls = harness(tmp_path, monkeypatch)
    definition['experiment']['run_deterministic'] = False
    with pytest.raises(ValueError, match='frozen model planning costs'):
        run()
    assert not calls
