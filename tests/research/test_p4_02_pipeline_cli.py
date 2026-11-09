"""Configuration, immutability and receipt contracts for the research CLI."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from factorlab.errors import ConfigurationError

from tools.research.pipeline_config import configure, identity, load_config, validate
from tools.research.run_pipeline import chinese_summary, main, run, verify

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'configs/research/p4-02-synthetic.json'


@pytest.fixture
def document():
    return load_config(EXAMPLE)


@pytest.mark.parametrize('section,key,value', [
    ('models', 'unknown', {}), ('portfolio', 'objective', 'mean_variance'),
    ('portfolio', 'investment', 1.1), ('portfolio', 'max_weight', .001),
    ('portfolio', 'turnover_limit', .79), ('portfolio', 'turnover_limit', .8),
    ('execution', 'initial_cash', 0), ('execution', 'initial_cash', True),
    ('execution', 'mode', 'guess'), ('costs', 'slippage_bps', -1),
    ('split', 'purge_days', 0), ('split', 'embargo_days', 0),
    ('split', 'step_months', 0), ('label', 'entry_price', 'same_close'),
    ('research', 'risk_lookback', 1), ('research', 'rebalance_sessions', 0),
    ('data', 'seed', 1), ('data', 'version', 'unbound'),
])
def test_reject_invalid_fields(document, section, key, value):
    document[section][key] = value
    with pytest.raises(ValueError):
        validate(document)


@pytest.mark.parametrize('change', ['real', 'unknown', 'factor', 'version', 'model_version', 'tree', 'alpha', 'duplicate'])
def test_reject_identity_and_model_errors(document, change, tmp_path):
    if change == 'real':
        document['source_kind'] = 'REAL_CANDIDATE'
    elif change == 'unknown':
        document['unexpected'] = 'field'
    elif change == 'factor':
        document['factors'][0]['name'] = 'FUTURE_CLOSE'
    elif change == 'version':
        document['factors'][0]['version'] = '999'
    elif change == 'model_version':
        document['models']['ridge']['version'] = '999'
    elif change == 'tree':
        document['models']['lightgbm']['parameters']['max_depth'] = 2
    elif change == 'alpha':
        document['models']['ridge']['parameters']['alpha'] = -1
    else:
        document['factors'].append(deepcopy(document['factors'][0]))
    with pytest.raises((ValueError, ConfigurationError)):
        configure(document, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('start,end', [('2023-04-03', '2024-01-12'),
                                      ('2023-12-15', '2023-04-03'),
                                      ('2023-01-02', '2023-12-15'),
                                      ('2023-04-02', '2023-12-15')])
def test_reject_time_windows(document, tmp_path, start, end):
    document['research'].update(start=start, end=end)
    with pytest.raises(ValueError):
        configure(document, tmp_path)


def test_reject_duplicate_json_and_nonfinite(tmp_path):
    for text in ('{"schema_version":1,"schema_version":1}', '{"x":NaN}'):
        path = tmp_path / 'invalid.json'
        path.write_text(text, encoding='utf-8')
        with pytest.raises(ValueError):
            load_config(path)


def test_conversion_fixed_models_and_feedback(document, tmp_path):
    document['execution']['mode'] = 'incremental_feedback'
    document['portfolio']['turnover_limit'] = .8
    config, _, _ = configure(document, tmp_path)
    assert config.execution_mode == 'incremental_feedback'
    assert config.optimizer.turnover_limit == .8
    assert config.models['ridge']['ridge_alphas'] == (1.,)
    assert len(config.models['lightgbm']['lightgbm_candidates']) == 1
    assert len(config.models['xgboost']['xgboost_candidates']) == 1
    assert all(options['factor_selector'] for options in config.models.values())


def test_seeded_provider_reproducible(document, tmp_path):
    left, _, left_identity = configure(document, tmp_path / 'a')
    right, _, right_identity = configure(document, tmp_path / 'b')
    assert left_identity == right_identity
    assert left.models.keys() == right.models.keys()
    assert identity(document) == identity(json.loads(json.dumps(document, sort_keys=True)))


def test_missing_psutil_rejected_before_research(monkeypatch, tmp_path):
    import builtins

    original_import = builtins.__import__

    def without_psutil(name, *args, **kwargs):
        if name == 'psutil':
            raise ModuleNotFoundError("No module named 'psutil'", name='psutil')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_psutil)
    output = tmp_path / 'missing-psutil'
    with pytest.raises(ConfigurationError, match='pip install -r requirements/scalable-compute.txt'):
        main(['--config', str(EXAMPLE), '--output', str(output)])
    assert not output.exists()


def test_existing_output_rejected_without_overwrite(tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    marker = target / 'marker'
    marker.write_text('preserve', encoding='utf-8')
    with pytest.raises(FileExistsError):
        run(EXAMPLE, target)
    assert marker.read_text(encoding='utf-8') == 'preserve'


def test_cli_requires_config(tmp_path):
    with pytest.raises(SystemExit):
        main(['--output', str(tmp_path / 'out')])


def test_summary_separates_estimands():
    text = chinese_summary(dict(execution_mode='batch', objective='minimum_variance', models={}))
    for term in ('预测能力', '组合收益', '交易成本', '统计显著性', '合成数据限制',
                 'NOT_DETERMINED', 'FORMAL_ADMISSION_BLOCKED', 'NOT FINANCIAL EVIDENCE'):
        assert term in text


def test_workflow_receipts_and_negative_integrity(tmp_path):
    """Exercise real immutable records with explicit untrained receipt fixtures."""
    from factorlab.dataset.identity import file_hash
    from factorlab.experiments import ExperimentTracker
    from tools.research.run_pipeline import write_json

    output = tmp_path
    document = load_config(EXAMPLE)
    write_json(output / 'normalized-config.json', document)
    write_json(output / 'identity.json', {'data': {'fixture': 'receipt-only'}})
    summary = dict(execution_mode='batch', objective='minimum_variance', models={})
    write_json(output / 'summary.json', summary)
    (output / 'SUMMARY.zh-CN.md').write_text(chinese_summary(summary), encoding='utf-8')
    for name in ('reproduction.json', 'resources.json'):
        write_json(output / name, {'status': 'NOT_MEASURED'})
    (output / 'comparison.csv').write_text('model\n', encoding='utf-8')
    tracker = ExperimentTracker(output / 'experiments')
    definition = dict(model='receipt-fixture', parameters={}, factors=[],
        dataset={'sha256': 'b' * 64}, label={'name': 'EXCESS_RET5'},
        windows={'train': None, 'validation': None, 'test': None}, walk_forward={}, costs={})
    options = dict(code={'commit': 'c' * 40, 'tree': 'd' * 40},
        environment={'python': 'receipt-fixture'}, command=['receipt-fixture'],
        status='NOT_RUN', qualification='SYNTHETIC')
    run_ids = list(document['models'])
    for name in run_ids:
        tracker.create_experiment(name, definition)
        tracker.append_run(name, name, **options)
    workflow_id = 'receipt-workflow'
    definition = dict(definition, parameters=dict(config_sha256=identity(document),
        configuration=document, data_identity={'fixture': 'receipt-only'}, run_ids=run_ids))
    tracker.create_experiment(workflow_id, definition)
    tracker.append_run(workflow_id, workflow_id, **options, artifacts={
        name: dict(path=str(output / name), sha256=file_hash(output / name))
        for name in ('summary.json', 'normalized-config.json', 'identity.json', 'SUMMARY.zh-CN.md')})
    write_json(output / 'receipt.json', dict(workflow_run_id=workflow_id,
        config_sha256=identity(document), dataset_identity='b' * 64, model_run_ids=run_ids,
        files={str(path.relative_to(output)): file_hash(path)
               for path in output.iterdir() if path.is_file()}))
    receipt = verify(output)
    assert len(receipt['model_run_ids']) == 4
    manifest_path = output / 'receipt.json'
    original_manifest = manifest_path.read_bytes()
    for key, value, error in (
        ('config_sha256', '0' * 64, 'configuration identity mismatch'),
        ('dataset_identity', '0' * 64, 'dataset identity mismatch'),
        ('model_run_ids', run_ids[:-1], 'model run identity mismatch'),
    ):
        try:
            write_json(manifest_path, dict(receipt, **{key: value}))
            with pytest.raises(ValueError, match=error):
                verify(output)
        finally:
            manifest_path.write_bytes(original_manifest)
    for name in ('summary.json', 'normalized-config.json', 'identity.json'):
        path = output / name
        original = path.read_bytes()
        try:
            path.write_bytes(original + b' ')
            with pytest.raises(ValueError):
                verify(output)
        finally:
            path.write_bytes(original)
    path = output / 'SUMMARY.zh-CN.md'
    original = path.read_bytes()
    try:
        path.unlink()
        with pytest.raises(ValueError):
            verify(output)
    finally:
        path.write_bytes(original)
    verify(output)


def test_ridge_reproduction_and_mode_contract(tmp_path):
    """Train only Ridge on local synthetic features; reuse scores for both modes."""
    import numpy as np
    import pandas as pd
    from factorlab.application.research_pipeline import prediction_targets, feedback_execution
    from factorlab.backtest import AShareExecutionBacktester
    from factorlab.dataset.partitioned import PartitionedDataset
    from factorlab.portfolio import PortfolioOptimizer
    from factorlab.validation import WalkForwardRunner
    from examples.research.research_pipeline_synthetic import configured

    from dataclasses import replace
    from factorlab.application.research_pipeline import ResearchPipeline
    from tools.research.run_pipeline import summarize

    output = tmp_path / 'ridge-source'
    document = load_config(EXAMPLE)
    config, hub, _ = configure(document, output)
    config = replace(config, models={'ridge': config.models['ridge']})
    from factorlab.experiments import ExperimentTracker

    result = ResearchPipeline(config).run(hub, tracker=ExperimentTracker(output / 'experiments'),
        repo_root=ROOT, command=['pytest', 'ridge-reproduction'])
    summary = summarize(result, config)
    _, _, provider = configured(tmp_path)
    dates = provider.tables['calendar']
    research_dates = dates[(dates >= config.research_start) & (dates <= config.research_end)]
    dataset = PartitionedDataset(output / 'dataset')
    walk = WalkForwardRunner(config.splitter, model_name='ridge', feature_names=config.builder.factor_names,
        top_k=config.top_k, minimum_cross_section=config.minimum_cross_section,
        **config.models['ridge']).run_partitioned(dataset, calendar=research_dates)
    returns = provider.tables['daily_bars'].pivot(index='date', columns='symbol', values='raw_close').sort_index().pct_change(fill_method=None).iloc[1:]
    allocation = dict(model_name='ridge', optimizer=PortfolioOptimizer(config.optimizer),
                      lookback=config.risk_lookback, every=config.rebalance_sessions)
    targets, repeated_decisions = prediction_targets(walk.predictions, returns, **allocation)
    saved_decisions = pd.read_csv(output / 'ridge/decisions.csv')
    assert repeated_decisions.prediction_identity.tolist() == saved_decisions.prediction_identity.tolist()
    from factorlab.metrics import evaluate_predictions
    prediction = evaluate_predictions(walk.predictions, target_col='EXCESS_RET5', top_k=10,
                                      minimum_cross_section=5, fold_col='fold')
    for metric, value in summary['models']['ridge']['prediction_metrics'].items():
        np.testing.assert_allclose(prediction.overall.iloc[0][metric], value['value'], rtol=1e-12, atol=1e-12)
    saved = pd.read_csv(output / 'ridge/targets.csv', parse_dates=['date'])
    pd.testing.assert_frame_equal(targets, saved, check_dtype=False, atol=1e-12, rtol=1e-12)
    first, last = walk.predictions.date.min(), walk.predictions.date.max()
    execution_dates = dates[(dates >= first) & (dates <= dates[dates.get_loc(last) + 1])]
    bars = provider.tables['daily_bars'].loc[lambda f: f.date.isin(execution_dates)].merge(
        provider.tables['security_status'], on=['date', 'symbol'], validate='one_to_one')
    benchmark = provider.tables['index_bars'].loc[lambda f: f.date.isin(execution_dates)]
    batch = AShareExecutionBacktester(config.execution, fee_schedule=config.fees).run_target_weights(targets, bars, benchmark)
    repeated_nav = pd.read_csv(output / 'ridge/nav.csv', parse_dates=['date', 'signal_date'])
    pd.testing.assert_frame_equal(batch.nav, repeated_nav, check_dtype=False, atol=1e-10, rtol=1e-12)
    feedback_targets, decisions, incremental = feedback_execution(walk.predictions, returns,
        engine=AShareExecutionBacktester(config.execution, fee_schedule=config.fees),
        bars=bars, benchmark=benchmark, **allocation)
    pd.testing.assert_frame_equal(targets, feedback_targets)
    for column in ('strategy_nav', 'cash', 'portfolio_value', 'transaction_cost'):
        np.testing.assert_allclose(batch.nav[column], incremental.nav[column], rtol=1e-12, atol=1e-10)
    assert (decisions.execution_time > decisions.decision_time).all()
