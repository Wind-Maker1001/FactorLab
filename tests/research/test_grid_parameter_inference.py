import json

import numpy as np
import pytest
from scipy.stats import t

from factorlab.experiments.grid_parameter_inference import (
    MODELS, assess_validation, select_refinement, summarize_stage,
)


def _summary(value, risk=.1, fills=1):
    return {'net_return': {'mean': value, 'quantiles': {'0.99': value}},
            'maximum_drawdown': {'mean': risk, 'quantiles': {'0.99': risk}},
            'maximum_committed': {'mean': 10., 'quantiles': {'0.99': 10.}},
            'fills': {'mean': fills, 'quantiles': {'0.99': fills}},
            'cvar95': risk, 'cvar99': risk, 'loss_probability': risk,
            'n': 100, 'fill_probability': float(fills > 0),
            'profit_per_mean_committed': value * 100}


def _write(root, model, replicate, value, *, fills=1, extra=None):
    metrics = ['net_return', 'maximum_drawdown', 'maximum_committed', 'fills']
    summaries = {'candidate': _summary(value, fills=fills),
                 'baseline': _summary(.01), 'arithmetic-baseline': _summary(.02),
                 'cash': _summary(0, risk=0, fills=0),
                 'candidate-hold': _summary(.03), 'candidate-endowed': _summary(.04)}
    summaries.update(extra or {})
    cell = {'status': 'COMPLETE', 'stage': 'D', 'model': model,
            'replicate': replicate, 'seed': MODELS.index(model) * 100 + replicate,
            'metrics': metrics, 'summaries': summaries,
            'paired_vs_baseline': {'candidate': {metric: {
                'mean_difference': summaries['candidate'][metric]['mean'] - summaries['baseline'][metric]['mean'],
                'path_standard_error': 1e6} for metric in metrics}},
            'paired_vs_controls': {'candidate': {control: {metric: {
                'mean_difference': summaries['candidate'][metric]['mean'] - summaries[other][metric]['mean'],
                'path_standard_error': 1e6} for metric in metrics}
                for control, other in [('cash', 'cash'), ('hold', 'candidate-hold'),
                                       ('endowed', 'candidate-endowed')]}}}
    directory = root / f'D-{model}-r{replicate}'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'status.json').write_text(json.dumps(cell), encoding='utf-8')
    return directory


def _validation(root, values, *, fills=1):
    for model in MODELS:
        for replicate, value in enumerate(values):
            _write(root, model, replicate, value, fills=fills)


def test_equal_model_seed_weights_without_path_arrays(tmp_path):
    _write(tmp_path, MODELS[0], 0, 0)
    _write(tmp_path, MODELS[0], 1, 2)
    _write(tmp_path, MODELS[1], 0, 5)
    failed = tmp_path / 'D-jump_down-r0'
    failed.mkdir()
    (failed / 'status.json').write_text(json.dumps({'status': 'FAILED'}))
    result = summarize_stage(tmp_path)
    assert result['candidate']['net_return']['mean'] == 3
    assert result['candidate']['net_return']['quantiles']['0.99'] == 3


def test_refinement_excludes_controls_and_uses_pareto_layers(tmp_path):
    extra = {'better': _summary(.5, risk=.01), 'worse': _summary(-.5, risk=.5)}
    _write(tmp_path, MODELS[0], 0, .1, extra=extra)
    assert select_refinement(tmp_path, count=1) == ['better']
    assert select_refinement(tmp_path, count=20) == ['better', 'candidate', 'worse']


def test_diverse_representatives_cover_tradeoff_extremes(tmp_path):
    extra = {f'choice{i}': _summary(i / 100, risk=i / 100) for i in range(1, 31)}
    _write(tmp_path, MODELS[0], 0, -.5, extra=extra)
    selected = select_refinement(tmp_path, count=20)
    assert len(selected) == 20
    assert {'choice1', 'choice30'} <= set(selected)
    assert 'candidate' not in selected


def test_validation_refuses_missing_or_failed_seed(tmp_path):
    _validation(tmp_path, [.1] * 5)
    status = tmp_path / 'D-gbm-r4' / 'status.json'
    status.write_text(json.dumps({'status': 'FAILED'}))
    with pytest.raises(ValueError, match='exactly five seeds'):
        assess_validation(tmp_path, ['candidate'])


def test_bonferroni_seed_t_can_reject_nominal_positive_result(tmp_path):
    values = [.01, .02, .03, .04, .05]
    _validation(tmp_path, values)
    report = assess_validation(tmp_path, ['candidate'])
    result = report['configurations']['candidate']['models']['gbm']
    nominal = result['metrics']['net_return']
    corrected = result['paired_vs_controls']['cash']['net_return']
    se = np.std(values, ddof=1) / np.sqrt(5)
    assert nominal['lower'] > 0
    assert corrected['lower'] == pytest.approx(.03 - t.ppf(1 - .05 / 4000, 4) * se)
    assert corrected['upper'] == pytest.approx(.03 + t.ppf(1 - .05 / 4000, 4) * se)
    assert corrected['seed_standard_error'] == pytest.approx(se)
    assert corrected['comparisons'] == 2000
    assert corrected['degrees_of_freedom'] == 4
    assert not report['configurations']['candidate']['passes']
    assert report['final_ids'] == []
    assert result['paired_vs_controls']['baseline']['net_return']['mean'] == pytest.approx(.02)
    assert result['paired_vs_controls']['hold']['net_return']['mean'] == pytest.approx(0)
    assert result['paired_vs_controls']['endowed']['net_return']['mean'] == pytest.approx(-.01)


def test_positive_nonstress_cash_evidence_passes_but_activity_is_required(tmp_path):
    _validation(tmp_path, [.999, 1, 1.001, 1.002, 1.003])
    assert assess_validation(tmp_path, ['candidate'])['final_ids'] == ['candidate']
    _write(tmp_path, 'gbm', 4, 1, fills=0)
    report = assess_validation(tmp_path, ['candidate'])
    assert report['final_ids'] == []
    assert not report['configurations']['candidate']['models']['gbm']['active']


def test_cash_pairing_falls_back_to_same_seed_means(tmp_path):
    _validation(tmp_path, [.1, .11, .12, .13, .14])
    path = tmp_path / 'D-gbm-r0' / 'status.json'
    cell = json.loads(path.read_text())
    del cell['paired_vs_controls']
    path.write_text(json.dumps(cell))
    report = assess_validation(tmp_path, ['candidate'], searched=1)
    result = report['configurations']['candidate']['models']['gbm']
    assert result['paired_vs_controls']['cash']['net_return']['mean'] == pytest.approx(.12)
    assert result['paired_vs_controls']['cash']['net_return']['comparisons'] == 4


def test_one_nonstress_model_negative_refuses_candidate(tmp_path):
    _validation(tmp_path, [.1] * 5)
    for replicate in range(5):
        _write(tmp_path, 'regime_historical', replicate, -.01)
    assert assess_validation(tmp_path, ['candidate'])['final_ids'] == []


def test_adverse_jump_losses_and_inactivity_are_reported_without_veto(tmp_path):
    _validation(tmp_path, [.1] * 5)
    for replicate in range(5):
        _write(tmp_path, 'jump_down', replicate, -.2, fills=0)
    report = assess_validation(tmp_path, ['candidate'])
    assert report['final_ids'] == ['candidate']
    stress = report['configurations']['candidate']['models']['jump_down']
    assert stress['metrics']['net_return']['mean'] == pytest.approx(-.2)
    assert stress['paired_vs_controls']['cash']['net_return']['upper'] < 0
    assert not stress['shortlist_gate']
    assert not stress['passes']
    assert not stress['active']
    assert report['cash_family_comparisons'] == 2000
