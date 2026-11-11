"""Causal candidate, frozen inference and shared-account contracts."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.dataset.identity import file_hash, json_hash
from factorlab.research.real_model_study import KEYS, TARGET, build_panel, make_runner
from factorlab.research.oos_execution_study import (
    signal_candidates, verify_predictions, compare_reconstruction, FittedFoldCapture,
    make_targets, execution_bars, valuation_gaps,
)
from factorlab.validation.factor_selection import TrainFactorSelector, FactorSelectionConfig
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.strategies.real_shared_portfolio import audit_ledger
from factorlab.errors import SchemaError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fixture():
    training = json.loads((ROOT / 'configs/research/p8-02-real-models.json').read_text())
    training.update(minimum_cross_section=4, top_k=2,
        splitter=dict(initial_train_months=3, validation_months=1, test_months=2,
            step_months=12, purge_days=6, embargo_days=6))
    training['selection'].update(minimum_cross_section=4, minimum_dates=20, k=3)
    dates = pd.bdate_range('2020-01-01', periods=260)
    rng = np.random.default_rng(100)
    rows = []
    for i in range(8):
        prices = (10+i) * np.exp(np.cumsum(rng.normal(0, .012, len(dates))))
        rows.extend(dict(date=d, symbol=f'{i:06d}.SZ', raw_open=p*.999,
            raw_high=p*1.02, raw_low=p*.98, raw_close=p,
            volume=float(rng.integers(100, 10000)), suspended=0, st_status=0)
            for d,p in zip(dates, prices))
    bars = pd.DataFrame(rows)
    members = bars.loc[bars.date.ge(dates[65]), KEYS].sort_values(KEYS).reset_index(drop=True)
    return bars, members, dates, training


def config():
    return json.loads((ROOT / 'configs/research/p9-01-oos-execution.json').read_text())


def test_prefix_executes_frozen_targets_without_terminal_rewrite(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('p9_prefix_executor', ROOT / 'tools/research/run_oos_execution.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    dates = pd.bdate_range('2023-03-13', periods=20)
    scores = pd.DataFrame(dict(date=dates, symbol='000540.SZ', score=1.))
    cfg = dict(config(), top_k=1, minimum_cross_section=1)
    targets, decisions = make_targets(scores, dates, ['000540.SZ'], cfg, 'B_lightgbm')
    prefix = dates[:9]
    frozen = targets.loc[targets.date.isin(prefix)].reset_index(drop=True)
    plan = (frozen, decisions.loc[decisions.date.isin(prefix)])
    rebuilt, _ = make_targets(scores, prefix, ['000540.SZ'], cfg, 'B_lightgbm')
    assert frozen.iloc[-1].target_weight == .1 and rebuilt.iloc[-1].target_weight == 0
    def forbidden(*_, **__):
        pytest.fail('Frozen prefix must not regenerate terminal targets')
    monkeypatch.setattr(runner, 'make_targets', forbidden)
    bars = pd.DataFrame(dict(date=prefix, symbol='000540.SZ', raw_open=10., raw_close=10.,
        volume=1000000, suspended=0, trade_status=1, is_st=0))
    result, metrics, _ = runner.execute(scores, bars, prefix, ['000540.SZ'], cfg,
        'B_lightgbm', tmp_path / 'prefix', dataset_id='synthetic', target_plan=plan)
    assert metrics['ledger']['status'] == 'PASSED'
    assert result.positions.loc[result.positions.date.eq(prefix[-1]), 'shares'].sum() > 0
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / 'prefix/targets.parquet'), frozen)


def test_prediction_byte_drift_rejected_before_read(tmp_path):
    path = tmp_path / 'predictions.parquet'
    path.write_bytes(b'not original')
    with pytest.raises(ValueError, match='SHA256_DRIFT'):
        verify_predictions(path, config())


def test_original_fold_identity_not_inferred_from_filename(tmp_path):
    path = tmp_path / 'predictions.parquet'
    pd.DataFrame(dict(model_id=['B_ridge'], fold=[99])).to_parquet(path)
    with pytest.raises(ValueError, match='IDENTITY_MISMATCH'):
        verify_predictions(path, dict(config(), prediction_sha256=file_hash(path)))


def test_candidate_pool_and_targets_ignore_future_label_status(fixture):
    bars, members, dates, training = fixture
    day = dates[150]
    before = signal_candidates(bars, members, dates)
    changed = bars.copy()
    changed.loc[changed.date.isin(dates[151:157]) & changed.symbol.eq('000000.SZ'), ['suspended', 'st_status']] = [1, 1]
    after = signal_candidates(changed, members, dates)
    pd.testing.assert_frame_equal(before.loc[before.date.le(day)], after.loc[after.date.le(day)])
    labelled, _, _, _ = build_panel(changed, members, dates, training)
    assert '000000.SZ' not in set(labelled.loc[labelled.date.eq(day), 'symbol'])
    assert '000000.SZ' in set(after.loc[after.date.eq(day), 'symbol'])
    cfg = dict(config(), top_k=2, target_weight=.1, minimum_cross_section=4)
    calendar = dates[150:170]
    a, _ = make_targets(before.assign(score=before.RAW_MOM20), calendar, bars.symbol.unique(), cfg, 'momentum20')
    b, _ = make_targets(after.assign(score=after.RAW_MOM20), calendar, bars.symbol.unique(), cfg, 'momentum20')
    pd.testing.assert_frame_equal(a.loc[a.date.eq(day)], b.loc[b.date.eq(day)])


def test_signal_features_exactly_reproduce_original_before_label_filter(fixture):
    bars, members, dates, training = fixture
    common, _, _, refs = build_panel(bars, members, dates, training)
    candidates = signal_candidates(bars, members, dates)
    names = [r.factor_id for r in refs]
    recovered = common[KEYS].merge(candidates, on=KEYS, validate='one_to_one')
    pd.testing.assert_frame_equal(common[KEYS + names], recovered[KEYS + names])


def test_fold_models_are_train_only_and_reconstructed_test_matches(fixture):
    bars, members, dates, training = fixture
    data, _, snapshot, refs = build_panel(bars, members, dates, training)
    names = tuple(r.factor_id for r in refs)
    selector = TrainFactorSelector(snapshot, refs, FactorSelectionConfig(**training['selection']))
    calendar = dates[dates >= data.date.min()]
    capture = FittedFoldCapture(make_runner(training, 'ridge', names, selector))
    result = capture.run(data, calendar)
    assert set(capture.fitted) == {0}
    original = result.predictions.rename(columns={'date':'signal_date', 'score':'prediction'})
    assert compare_reconstruction(original, result.predictions)[0]['matched']
    changed = result.predictions.copy()
    changed.loc[0, 'score'] += 1
    with pytest.raises(ValueError, match='PREDICTION_MISMATCH'):
        compare_reconstruction(original, changed)
    changed = result.predictions.copy()
    changed.loc[0, 'fold'] = 9
    with pytest.raises(ValueError, match='KEYS_MISMATCH'):
        compare_reconstruction(original, changed)
    selection = result.factor_selection.iloc[0]
    assert pd.Timestamp(selection.train_max_exit_date) < pd.Timestamp(selection.validation_start)
    assert pd.Timestamp(selection.validation_max_exit_date) < pd.Timestamp(selection.test_start)


def test_fixed_five_session_schedule_does_not_reanchor_missing_signal():
    dates = pd.bdate_range('2024-01-01', periods=18)
    symbols = [f'{i:06d}.SZ' for i in range(20)]
    scores = pd.DataFrame([dict(date=d, symbol=s, score=i) for d in dates[1:] for i,s in enumerate(symbols)])
    targets, decisions = make_targets(scores, dates, symbols, config(), 'B_ridge')
    assert targets.date.drop_duplicates().tolist() == list(dates[[0, 5, 10, 15]])
    assert targets.loc[targets.date.eq(dates[0]), 'target_weight'].eq(0).all()
    assert targets.loc[targets.date.eq(dates[15]), 'target_weight'].eq(0).all()
    assert targets.loc[targets.date.eq(dates[10]), 'target_weight'].gt(0).sum() == 10
    assert targets.groupby('date').target_weight.sum().le(1.).all()
    assert decisions.iloc[0].reason == 'MISSING_OR_SPARSE_SIGNAL_CASH'
    assert decisions.iloc[-1].reason == 'TERMINAL_ZERO_TARGET'


def test_hash_baseline_independent_of_model_scores():
    dates = pd.bdate_range('2024-01-01', periods=12)
    symbols = [f'{i:06d}.SZ' for i in range(20)]
    scores = pd.DataFrame([dict(date=d, symbol=s, score=i) for d in dates for i,s in enumerate(symbols)])
    a, _ = make_targets(scores, dates, symbols, config(), 'hash_equal')
    b, _ = make_targets(scores.assign(score=-scores.score), dates, symbols, config(), 'hash_equal')
    pd.testing.assert_frame_equal(a, b)


def test_next_open_shared_cash_held_missing_close_and_terminal_residual():
    dates = pd.bdate_range('2024-01-01', periods=12)
    symbols = ['000001.SZ', '000002.SZ']
    bars = pd.DataFrame([dict(date=d, symbol=s, raw_open=10., raw_close=10., volume=10000., suspended=0, st_status=0) for d in dates for s in symbols])
    scores = bars[KEYS].assign(score=1.)
    cfg = dict(config(), top_k=2, target_weight=.5, minimum_cross_section=2)
    targets, _ = make_targets(scores, dates, symbols, cfg, 'B_ridge')
    reference = pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.))
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=10010,
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'), FeeSchedule.a_share_default())
    prepared = execution_bars(bars)
    result = engine.run_target_weights(targets, prepared, reference)
    audit_ledger(result, prepared, 10010)
    assert result.trades.execution_date.min() == dates[1]
    assert result.trades.loc[result.trades.side.eq('sell'), 'execution_date'].min() == dates[6]
    assert result.positions.loc[result.positions.date.eq(dates[-1])].empty
    assert (result.trades.executed_shares % 100 == 0).all()
    assert result.nav.cash.ge(0).all()
    prepared.loc[prepared.date.eq(dates[2]) & prepared.symbol.eq(symbols[0]), 'raw_close'] = np.nan
    stale = engine.run_target_weights(targets, prepared, reference)
    gaps = valuation_gaps(stale, prepared)
    assert gaps[['date', 'symbol']].to_dict('records') == [dict(date=dates[2], symbol=symbols[0])]
    assert cfg['total_return'] is None
    assert cfg['total_return_status'] == 'TOTAL_RETURN_UNAVAILABLE'
    prepared.loc[prepared.date.ge(dates[6]), 'trade_status'] = 0
    residual = engine.run_target_weights(targets, prepared, reference)
    assert not residual.positions.loc[residual.positions.date.eq(dates[-1])].empty
    assert 'suspended' in set(residual.rejections.reason)


def test_missing_held_execution_status_is_a_real_engine_boundary():
    dates = pd.bdate_range('2024-01-01', periods=3)
    bars = pd.DataFrame(dict(date=dates, symbol='000540.SZ', raw_open=10., raw_close=10.,
        volume=10000., trade_status=[1, 1, np.nan], is_st=[0, 0, np.nan]))
    targets = pd.DataFrame(dict(date=dates[:2], symbol='000540.SZ', target_weight=[.1, 0.]))
    reference = pd.DataFrame(dict(date=dates, raw_open=1., raw_close=1.))
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(
        benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'))
    with pytest.raises(SchemaError, match='execution status is missing for 2024-01-03/000540.SZ'):
        engine.run_target_weights(targets, bars, reference)


def test_task_adapter_never_evaluates_unobserved_nav(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('p9_runner', ROOT / 'tools/research/run_oos_execution.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    dates = pd.bdate_range('2024-01-01', periods=12)
    symbols = ['000001.SZ', '000002.SZ']
    bars = pd.DataFrame([dict(date=d, symbol=s, raw_open=10., raw_close=10., volume=10000.,
        trade_status=1, is_st=0) for d in dates for s in symbols])
    scores = bars[KEYS].assign(score=1.)
    cfg = dict(config(), top_k=2, target_weight=.5, minimum_cross_section=2)
    _, metrics, nav = runner.execute(scores, bars, dates, symbols, cfg, 'B_ridge',
        tmp_path / 'valid', dataset_id='SYNTHETIC_TEST')
    assert metrics['ledger']['status'] == 'PASSED'
    assert metrics['total_return'] is None
    assert len(nav) == len(dates)
    bars.loc[bars.date.eq(dates[2]) & bars.symbol.eq(symbols[0]), 'raw_close'] = np.nan
    _, metrics, nav = runner.execute(scores, bars, dates, symbols, cfg, 'B_ridge',
        tmp_path / 'partial', dataset_id='SYNTHETIC_TEST')
    assert metrics['status'] == 'PARTIAL'
    assert nav is None
    assert 'cumulative_return' not in metrics
    assert (tmp_path / 'partial/positions.parquet').exists()
    assert not (tmp_path / 'partial/daily-nav.csv').exists()


def test_publication_keeps_unavailable_returns_null_and_rejects_prediction_drift(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('p9_publisher', ROOT / 'tools/research/publish_oos_execution.py')
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    source = tmp_path / 'private'
    source.mkdir()
    cfg = config()
    (source / 'config.json').write_text(json.dumps(cfg))
    counts = pd.DataFrame([dict(model_id=m, fold=f, score=1.) for m in cfg['models'] for f in range(3)])
    counts.to_parquet(source / 'candidate-predictions.parquet')
    identity = dict.fromkeys(('kind', 'original_prediction_sha256', 'common_test_hash',
        'original_sample_hash', 'data_identity', 'feature_identity', 'candidate_hash',
        'training_config_hash'), 'SYNTHETIC')
    identity.update(candidate_rows=12, config_hash=json_hash(cfg), environment={}, code={},
        candidate_prediction_sha256=file_hash(source / 'candidate-predictions.parquet'),
        secret_runtime_path='PRIVATE_DO_NOT_PUBLISH')
    (source / 'inference-identity.json').write_text(json.dumps(identity))
    reconstruction = [dict(model_id=m, fold=f, matched=True) for m in cfg['models'] for f in range(3)]
    (source / 'reconstruction.json').write_text(json.dumps(reconstruction))
    (source / 'result.json').write_text(json.dumps(dict(status='P9_01_BLOCKED', reason='SYNTHETIC_BOUNDARY')))
    (source / 'pilot.json').write_text(json.dumps(dict(total_return=None)))
    (source / 'pilot').mkdir()
    pd.DataFrame(dict(date=['2024-01-01'], strategy_nav=[1.])).to_csv(source / 'pilot/daily-nav.csv', index=False)
    summary = publisher.publish(source, tmp_path / 'public')
    assert summary['total_return'] is None
    assert 'PRIVATE_DO_NOT_PUBLISH' not in json.dumps(summary)
    metrics = pd.read_csv(tmp_path / 'public/fold-metrics.csv')
    assert metrics.loc[metrics.model_id.ne('cash'), 'cumulative_return'].isna().all()
    assert metrics.loc[metrics.model_id.eq('cash'), 'cumulative_return'].eq(0).all()
    assert metrics.total_return.isna().all()
    assert set(pd.read_csv(tmp_path / 'public/daily-nav.csv').columns) == {'date', 'strategy_nav', 'model_id', 'fold', 'scope'}
    (source / 'candidate-predictions.parquet').write_bytes(b'drift')
    with pytest.raises(ValueError, match='prediction identity drift'):
        publisher.publish(source, tmp_path / 'drifted')
