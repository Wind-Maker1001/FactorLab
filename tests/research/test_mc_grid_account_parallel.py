"""Exercise the MC-05 scheduler with real spawned Paper accounts."""

from dataclasses import asdict, replace
from functools import partial
import csv
import json
import tempfile

import numpy as np
import pytest

from factorlab.experiments import mc_grid_tail_convergence as study
from factorlab.experiments.mc_grid_tail_f03_financial import capture_account_diagnostics
from tests.research.test_mc_grid_tail_convergence import settings
from tests.research.test_mc_regime_jump import grid, study_input


@pytest.fixture
def small_study(tmp_path, monkeypatch):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch))
    inputs, receipt, threshold, regime = study_input()
    inputs = replace(inputs, drift=[0.], volatility=[0.])
    definition, _, _ = settings()
    config, rules = grid()
    config = replace(config, lower=92., upper=108.)
    models = ['regime_historical', 'gbm']
    definition.update(models=models, grid=asdict(config), start='2026-11-15T00:00:00Z',
                      as_of=receipt['as_of'], threshold_as_of=threshold)
    definition['experiment'].update(
        coverage_first=True, run_deterministic=False, admission_unit='model_all_seeds',
        path_evidence=True, sizes=[2], batch_size=2, seeds=[2718, 1729], steps=16,
        resamples=9, budget_seconds=120,
    )
    definition['resource_planning'] = dict(models={model: dict(
        seconds_per_account_including_cleanup=1, group_generation_statistics_seconds=1,
    ) for model in models})
    monkeypatch.setattr(study, 'scenario_models', partial(study.scenario_models, regime_config=regime))
    run = partial(study.run_study, inputs=inputs, receipt=receipt, config=config, rules=rules,
                  definition=definition)
    return run, definition, scratch


def _read_json(output, name):
    return json.loads((output / name).read_text(encoding='utf-8'))


def _read_paths(output):
    with (output / 'paths.csv').open(newline='', encoding='utf-8') as rows:
        return list(csv.DictReader(rows))


def test_spawned_accounts_preserve_seed_order_and_research_evidence(small_study, tmp_path):
    run, definition, scratch = small_study
    serial, parallel = tmp_path / 'serial', tmp_path / 'parallel'
    serial_result = run(serial, max_workers=1)
    parallel_result = run(parallel, max_workers=2)

    expected_ids = [(model, str(seed), str(path_id)) for model in definition['models']
                    for seed in definition['experiment']['seeds'] for path_id in range(2)]
    serial_rows, parallel_rows = _read_paths(serial), _read_paths(parallel)
    assert [(row['model'], row['seed'], row['path_id']) for row in parallel_rows] == expected_ids
    for row in serial_rows + parallel_rows:
        del row['temporary_account_bytes']
    assert serial_rows == parallel_rows

    for name in ('groups.json', 'path-evidence.json'):
        assert _read_json(serial, name) == _read_json(parallel, name)
    tails = _read_json(parallel, 'tails.json')
    assert _read_json(serial, 'tails.json') == tails
    serial_generation, parallel_generation = (_read_json(output, 'generation.json')
                                              for output in (serial, parallel))
    for entry in serial_generation + parallel_generation:
        entry['metadata'].pop('elapsed_seconds', None)
    assert serial_generation == parallel_generation

    # The flat GBM paths tie on NAV. The configured first seed and path win the tie.
    assert tails['gbm'] == dict(
        terminal_nav=1000, identity=dict(model='gbm', seed=2718, path_id=0), prices=[100.] * 17,
    )
    for field in ('statistical_status', 'model_execution_status', 'completed_paths_per_seed_by_model',
                  'seed_dispersion', 'convergence'):
        assert serial_result[field] == parallel_result[field]
    assert parallel_result['performance']['accounts'] == len(expected_ids)
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('diagnostic', [None, capture_account_diagnostics], ids=['baseline', 'f03'])
def test_worker_failure_preserves_prior_group_and_cleans_accounts(small_study, tmp_path, monkeypatch, diagnostic):
    run, definition, scratch = small_study
    definition['models'] = ['gbm']
    definition['experiment'].update(sizes=[1, 2], batch_size=1, steps=4)

    def batches(model, inputs, generator, *, seed, begin, **kwargs):
        prices = np.full((1, 5), float(inputs.initial_prices[0]))
        if begin == 1 and seed == 1729:
            prices[0, -1] = -1.
        yield begin, prices, {}

    monkeypatch.setattr(study, 'batches', batches)
    output = tmp_path / 'worker-failure'
    with pytest.raises(ValueError, match='scenario price must be a finite positive Decimal'):
        run(output, max_workers=2, diagnostic=diagnostic)

    groups = _read_json(output, 'groups.json')
    assert [(cell['model'], cell['seed'], cell['n_paths'], cell['groups']['unconditional']['n'])
            for cell in groups] == [('gbm', 2718, 1, 1), ('gbm', 1729, 1, 1)]
    failure = _read_json(output, 'failure.json')
    assert failure['status'] == 'ACCOUNT_REPLAY_ABORTED'
    assert (failure['model'], failure['size'], failure['exception_type']) == ('gbm', 2, 'ValueError')
    assert failure['completed_paths_per_seed_by_model'] == {'gbm': 1}
    if diagnostic is not None:
        assert len(_read_paths(output)) == 3
        assert _read_json(output, 'tails.json')['gbm']['identity']['path_id'] == 0
    assert not (output / 'summary.json').exists()
    assert not list(scratch.iterdir())
