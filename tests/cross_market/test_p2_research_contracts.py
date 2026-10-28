"""Research entrypoint contracts: unchanged OOS, bounded execution, real identity."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market import research
from factorlab.cross_market.adapters import ChinaAshareAdapter, UnknownActions
from factorlab.cross_market.contracts import ContractError, ResearchSpec
from factorlab.cross_market.identity import DynamicUniverse, IdentityRegistry
from factorlab.cross_market.synthetic import build_us


def inputs(end='2024-04-02', market='US', universe=None):
    bars, adapter = build_us('2024-01-02', end, count=12)
    if market == 'CN':
        aliases = adapter.universe.identities.aliases.assign(market='CN', currency='CNY', exchange='XSHG')
        adapter.calendar.market = 'CN'
        adapter = ChinaAshareAdapter(adapter.calendar,
            DynamicUniverse(IdentityRegistry(aliases), 'CN', 'ALL_CN_A_SHARE'), adapter.corporate_actions)
    name = universe or {'US': 'ALL_US_COMMON', 'CN': 'ALL_CN_A_SHARE'}[market]
    spec = ResearchSpec(market, name, '2024-01-02', end, 'fixture-index', adapter.currency,
        factor_names=('REV1',), model=('FactorScore',), top_k=(2,), rebalance_frequency=1,
        train_months=1, validation_months=1, test_months=1)
    dates = adapter.calendar.sessions(spec.start_date, spec.end_date).index
    benchmark = pd.DataFrame(dict(date=dates, close=100 + np.arange(len(dates)) / 100,
        available_at=adapter.calendar.schedule.loc[dates, 'close'].to_numpy(),
        currency=adapter.currency, return_definition='price_return', benchmark_id=spec.benchmark))
    return bars, adapter, spec, benchmark


def run(data, path):
    bars, adapter, spec, benchmark = data
    return research.run_research(bars, adapter, spec, path, benchmark=benchmark, synthetic=True)


def fake_predictions(monkeypatch, adapter, dates):
    pred = pd.DataFrame([dict(date=pd.Timestamp(day), instrument_id=f'fixture:US:{i}',
        score=float(i), target=np.nan, signal_at=adapter.calendar.at(day, 'close'), fold=0)
        for day in dates for i in range(3)])
    def models(dataset, spec, calendar, output, estimators):
        output.mkdir(parents=True)
        pred.to_parquet(output / 'FactorScore-predictions.parquet', index=False)
        return {'FactorScore': pred.copy()}, [dict(model='FactorScore', fold=0)]
    monkeypatch.setattr(research, 'run_models', models)
    return pred


def assert_nav(path, end):
    fast = pd.read_parquet(path / 'FactorScore-top2-fast-nav.parquet')
    execution = pd.read_parquet(path / 'FactorScore-top2-execution-nav.parquet')
    benchmark = pd.read_parquet(path / 'benchmark.parquet')
    pd.testing.assert_series_equal(fast.date, execution.date)
    assert fast.date.max() == pd.Timestamp(end)
    expected = benchmark.set_index('date').loc[fast.date, 'close'].to_numpy()
    np.testing.assert_array_equal(fast.benchmark_nav, expected / expected[0])
    np.testing.assert_array_equal(fast.benchmark_nav, execution.benchmark_nav)
    trades = pd.read_parquet(path / 'FactorScore-top2-execution-trades.parquet')
    assert (trades.date > fast.date.min()).all()


def test_review_case_preserves_all_real_oos_and_blocks_only_boundary(tmp_path):
    data = inputs()
    result = run(data, tmp_path / 'run')
    assert result['status'] == 'PASS'
    pred = pd.read_parquet(tmp_path / 'run/models/FactorScore-predictions.parquet')
    dataset = pd.read_parquet(tmp_path / 'run/dataset.parquet')
    reference, folds = research.run_models(dataset, data[2], data[1].calendar, tmp_path / 'reference')
    pd.testing.assert_frame_equal(pred, reference['FactorScore'])
    assert pred.date.max() == pd.Timestamp(data[2].end_date)
    audit = pd.read_parquet(tmp_path / 'run/FactorScore-execution-eligibility.parquet')
    assert len(audit) == len(pred)
    assert audit.reason.eq('NEXT_EXECUTION_OUTSIDE_VALIDATED_COVERAGE').sum() == pred.date.eq(data[2].end_date).sum()
    assert_nav(tmp_path / 'run', data[2].end_date)


@pytest.mark.parametrize('dates,blocked', [(['2024-04-01'], 0), (['2024-04-01', '2024-04-02'], 3)])
def test_next_execution_inside_and_outside(monkeypatch, tmp_path, dates, blocked):
    data = inputs()
    original = fake_predictions(monkeypatch, data[1], dates)
    result = run(data, tmp_path / 'run')
    assert result['status'] == 'PASS'
    pd.testing.assert_frame_equal(original, pd.read_parquet(tmp_path / 'run/models/FactorScore-predictions.parquet'))
    audit = pd.read_parquet(tmp_path / 'run/FactorScore-execution-eligibility.parquet')
    assert (~audit.executable).sum() == blocked
    assert_nav(tmp_path / 'run', '2024-04-02')


@pytest.mark.parametrize('cause', ['only_boundary', 'action_end', 'bar_end', 'unknown_actions', 'no_next_calendar'])
def test_no_executable_interval_is_blocked_without_performance(monkeypatch, tmp_path, cause):
    bars, adapter, spec, benchmark = inputs()
    dates = ['2024-04-02'] if cause == 'only_boundary' else ['2024-04-01']
    if cause == 'action_end':
        adapter.corporate_actions.end = pd.Timestamp('2024-04-01')
    elif cause == 'bar_end':
        bars = bars.loc[bars.date.le('2024-04-01')]
    elif cause == 'unknown_actions':
        adapter.corporate_actions = UnknownActions()
    elif cause == 'no_next_calendar':
        adapter.calendar.schedule = adapter.calendar.schedule.loc[:'2024-04-01']
        spec = replace(spec, end_date='2024-04-01')
        benchmark = benchmark.loc[benchmark.date.le(spec.end_date)]
        bars = bars.loc[bars.date.le(spec.end_date)]
    fake_predictions(monkeypatch, adapter, dates)
    result = run((bars, adapter, spec, benchmark), tmp_path / 'run')
    assert result['status'] == 'BLOCKED'
    assert len(result['backtests']) == 2
    assert all(r['status'] == 'BLOCKED' and r['reason'] for r in result['backtests'])
    assert not list((tmp_path / 'run').glob('*-nav.parquet'))


def test_independently_validated_extra_inputs_do_not_extend_requested_horizon(monkeypatch, tmp_path):
    bars, adapter, spec, benchmark = inputs('2024-04-03')
    spec = replace(spec, end_date='2024-04-02')
    fake_predictions(monkeypatch, adapter, ['2024-04-01', '2024-04-02'])
    result = run((bars, adapter, spec, benchmark), tmp_path / 'run')
    assert result['status'] == 'PASS'
    assert_nav(tmp_path / 'run', '2024-04-02')


@pytest.mark.parametrize('defect', ['raw_close', 'adj_close', 'benchmark', 'actions'])
def test_invalid_required_inputs_never_become_pass(monkeypatch, tmp_path, defect):
    bars, adapter, spec, benchmark = inputs()
    fake_predictions(monkeypatch, adapter, ['2024-04-01'])
    if defect in {'raw_close', 'adj_close'}:
        bars.loc[bars.date.eq('2024-04-02'), defect] = np.nan
    elif defect == 'benchmark':
        benchmark.loc[benchmark.date.eq('2024-04-02'), 'close'] = np.nan
    else:
        adapter.corporate_actions.ids = frozenset()
    try:
        result = run((bars, adapter, spec, benchmark), tmp_path / 'run')
    except ContractError:
        return
    assert result['status'] != 'PASS'


def index_universe(adapter, name, dates):
    ids = adapter.universe.identities.aliases.instrument_id.unique()
    # CSI500 cardinality is checked by the original DynamicUniverse contract.
    if name == 'CSI500':
        base = adapter.universe.identities.aliases.iloc[0].to_dict()
        aliases = pd.DataFrame([{**base, 'instrument_id': f'cn:{i}', 'symbol': f'{i:06}'} for i in range(500)])
        adapter.universe.identities = IdentityRegistry(aliases)
        ids = aliases.instrument_id
    membership = pd.DataFrame([dict(date=d, instrument_id=i, available_at='2024-01-01T00:00Z')
        for d in dates for i in adapter.universe.identities.active(d, adapter.calendar.at(d, 'open')).instrument_id])
    adapter.universe = DynamicUniverse(adapter.universe.identities, adapter.market, name, membership)


@pytest.mark.parametrize('market', ['US', 'CN'])
@pytest.mark.parametrize('reverse', [False, True])
def test_universe_mismatch_rejected_before_any_artifact(monkeypatch, tmp_path, market, reverse):
    data = list(inputs(market=market))
    index = {'US': 'US_INDEX', 'CN': 'CSI500'}[market]
    if reverse:
        index_universe(data[1], index, data[3].date)
    else:
        data[2] = replace(data[2], universe=index)
    monkeypatch.setattr(research, 'build_dataset', lambda *a: pytest.fail('dataset created before identity rejection'))
    with pytest.raises(ContractError, match='adapter/spec|universe|identity|membership|members'):
        run(data, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('market', ['US', 'CN'])
@pytest.mark.parametrize('defect', ['renamed', 'empty', 'late', 'missing_day', 'adapter_spoof', 'universe_spoof', 'market', 'currency'])
def test_identity_disguise_and_missing_membership_rejected_early(monkeypatch, tmp_path, market, defect):
    data = list(inputs(market=market))
    adapter = data[1]
    index = {'US': 'US_INDEX', 'CN': 'CSI500'}[market]
    if defect in {'renamed', 'empty', 'late', 'missing_day'}:
        data[2] = replace(data[2], universe=index)
        if defect == 'renamed':
            adapter.universe.name = index
        else:
            index_universe(adapter, index, data[3].date)
            if defect == 'empty':
                adapter.universe.membership = adapter.universe.membership.iloc[:0]
            elif defect == 'late':
                adapter.universe.membership['available_at'] = '2025-01-01T00:00Z'
            else:
                adapter.universe.membership = adapter.universe.membership.loc[adapter.universe.membership.date.ne(data[3].date.iloc[3])]
    elif defect == 'adapter_spoof':
        data[1] = SimpleNamespace(**vars(adapter))
    elif defect == 'universe_spoof':
        adapter.universe = SimpleNamespace(**vars(adapter.universe), members=adapter.universe.members)
    elif defect == 'market':
        adapter.universe.market = 'CN' if market == 'US' else 'US'
    else:
        adapter.currency = 'CNY' if market == 'US' else 'USD'
    monkeypatch.setattr(research, 'build_dataset', lambda *a: pytest.fail('dataset created before identity rejection'))
    with pytest.raises(ContractError, match='adapter/spec|universe|identity|membership|members'):
        run(data, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('market', ['US', 'CN'])
@pytest.mark.parametrize('index', [False, True])
def test_legal_universes_pass_identity_gate(monkeypatch, tmp_path, market, index):
    data = list(inputs(market=market))
    if index:
        name = {'US': 'US_INDEX', 'CN': 'CSI500'}[market]
        index_universe(data[1], name, data[3].date)
        data[2] = replace(data[2], universe=name)
    class ReachedDataset(Exception):
        pass
    def reached(*args):
        raise ReachedDataset()
    monkeypatch.setattr(research, 'build_dataset', reached)
    with pytest.raises(ReachedDataset):
        run(data, tmp_path / 'accepted')


def test_legal_us_index_runs_both_engines_without_changing_admission(tmp_path):
    data = list(inputs())
    index_universe(data[1], 'US_INDEX', data[3].date)
    data[2] = replace(data[2], universe='US_INDEX')
    result = run(data, tmp_path / 'index')
    assert result['status'] == 'PASS'
    assert result['adapter_identity']['universe'] == 'US_INDEX'
    assert result['formal_admission'] == result['execution_admission'] == 'BLOCKED'
    assert_nav(tmp_path / 'index', '2024-04-02')


@pytest.mark.parametrize('limit', ['bars', 'actions'])
def test_valid_shorter_coverage_caps_both_engines(monkeypatch, tmp_path, limit):
    bars, adapter, spec, benchmark = inputs()
    fake_predictions(monkeypatch, adapter, ['2024-03-27', '2024-04-01', '2024-04-02'])
    if limit == 'bars':
        bars = bars.loc[bars.date.le('2024-03-28')]
    else:
        adapter.corporate_actions.end = pd.Timestamp('2024-03-28')
    result = run((bars, adapter, spec, benchmark), tmp_path / 'run')
    assert result['status'] == 'PASS'
    assert result['execution_signals'][0]['excluded_rows'] == 6
    assert_nav(tmp_path / 'run', '2024-03-28')


def test_nav_date_disagreement_blocks_performance(monkeypatch, tmp_path):
    data = inputs()
    fake_predictions(monkeypatch, data[1], ['2024-04-01'])
    original = research.fast_backtest
    def wrong_dates(*args):
        result = original(*args)
        result.nav = result.nav.iloc[:-1]
        return result
    monkeypatch.setattr(research, 'fast_backtest', wrong_dates)
    result = run(data, tmp_path / 'run')
    fast = next(r for r in result['backtests'] if r['mode'] == 'fast')
    assert fast['status'] == 'BLOCKED' and 'dates differ' in fast['reason']
    assert not (tmp_path / 'run/FactorScore-top2-fast-nav.parquet').exists()
