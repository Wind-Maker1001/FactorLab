from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market.contracts import ResearchSpec, ContractError
from factorlab.cross_market.synthetic import build_us
from factorlab.cross_market.engine import ExecutionEngine, target_weights, fast_backtest
from factorlab.cross_market.research import run_research, build_dataset
from factorlab.cross_market.research import run_models
from factorlab.cross_market.benchmark import validate_benchmark
from factorlab.cross_market.calendar import us_calendar
from factorlab.validation.splitter import ExpandingWalkForwardSplitter
from factorlab.cross_market.adapters import DatedFees, FeePeriod


def benchmark(adapter, spec):
    dates = adapter.calendar.sessions(spec.start_date, spec.end_date).index
    return pd.DataFrame(dict(date=dates, close=100. + np.arange(len(dates)) / 100,
        available_at=adapter.calendar.schedule.loc[dates, 'close'].to_numpy(), currency='USD',
        return_definition='price_return', benchmark_id=spec.benchmark))


def test_us_four_models_topk_execution_e2e(tmp_path):
    bars, a = build_us()
    s = ResearchSpec('US', 'ALL_US_COMMON', '2023-09-01', '2025-02-28', 'SYNTHETIC_PRICE_INDEX', 'USD',
                     train_months=3, validation_months=1, test_months=2)
    r = run_research(bars, a, s, tmp_path / 'us', benchmark=benchmark(a, s), synthetic=True, estimators=8)
    assert r['status'] in {'PASS', 'PARTIAL'}
    assert len(r['models']) == 4 and r['fold_count'] == 5
    assert len(r['backtests']) == 16
    execution = [x for x in r['backtests'] if x['mode'] == 'execution']
    assert len(execution) == 8 and all(x['status'] == 'PASS' and x['trades'] > 0 for x in execution)
    # Adjusted-price Fast has no terminal cash-action accounting and must refuse
    # an unpriced delisted holding. The required US acceptance is Execution.
    for x in r['backtests']:
        if x['status'] == 'BLOCKED':
            assert x['mode'] == 'fast' and 'valuation' in x['reason']
    assert r['formal_admission'] == r['execution_admission'] == 'BLOCKED'
    assert r['real_us_data'] == 'US_REAL_DATA_NOT_VALIDATED'
    for model in r['models']:
        nav = pd.read_parquet(tmp_path / 'us' / (model + '-top20-execution-nav.parquet'))
        assert (nav.cash >= -1e-8).all() and (nav.available_cash <= nav.cash + 1e-8).all()
        assert 'benchmark_nav' in nav


def test_future_signal_timestamp_reaches_execution_rejection():
    bars, a = build_us(count=10)
    scores = pd.DataFrame({'date': pd.to_datetime(['2024-06-03']), 'instrument_id': ['fixture:US:0'],
                           'score': [1.], 'signal_at': pd.to_datetime(['2024-06-04T22:00Z'])})
    targets = target_weights(scores, a.calendar, top_k=1)
    assert targets.signal_at.iloc[0] == scores.signal_at.iloc[0]
    with pytest.raises(ContractError, match='signal was unavailable'):
        ExecutionEngine(a).run(bars, targets, '2024-06-03', '2024-06-07')
    with pytest.raises(ContractError, match='signal was unavailable'):
        fast_backtest(bars, targets, a.calendar, a.fees, '2024-06-03', '2024-06-07')


def test_fast_refuses_short_or_leveraged_targets():
    bars, a = build_us(count=10)
    targets = pd.DataFrame(dict(date=pd.to_datetime(['2024-06-03']), instrument_id=['fixture:US:0'], target_weight=[-1.]))
    with pytest.raises(ContractError, match='long-only'):
        fast_backtest(bars, targets, a.calendar, a.fees, '2024-06-03', '2024-06-07')
    targets['target_weight'] = 2.
    with pytest.raises(ContractError, match='long-only'):
        fast_backtest(bars, targets, a.calendar, a.fees, '2024-06-03', '2024-06-07')


def test_fast_minimum_fees_cannot_create_negative_cash_or_phantom_sells():
    c = us_calendar('2024-06-03', '2024-06-07')
    dates = c.schedule.index
    rows = [dict(date=d, instrument_id=iid, adj_open=(1. if d <= pd.Timestamp('2024-06-04') else .1),
                 adj_close=(1. if d <= pd.Timestamp('2024-06-03') else .1)) for d in dates for iid in ['id-a', 'id-b']]
    bars = pd.DataFrame(rows)
    targets = pd.DataFrame(dict(date=pd.to_datetime(['2024-06-03', '2024-06-04']), instrument_id=['id-a', 'id-b'], target_weight=[1., 1.]))
    fees = DatedFees([FeePeriod('2024-06-01', '2024-06-30', minimum=5)], 'minimum-fee-test')
    r = fast_backtest(bars, targets, c, fees, dates.min(), dates.max(), initial_cash=10)
    assert (r.nav.cash >= 0).all() and (r.nav.nav >= 0).all()
    assert not (r.trades.side.eq('sell') & r.trades.instrument_id.eq('id-b')).any()
    assert 'INSUFFICIENT_CASH_FOR_FEES' in set(r.rejections.reason)


def test_execution_unfunded_sell_fee_preserves_lots():
    _, a = build_us(count=10)
    a.fees = DatedFees([FeePeriod('2024-01-01', '2024-12-31', minimum=5)], 'minimum-fee-test')
    e = ExecutionEngine(a, initial_cash=1)
    e.lots['fixture:US:0'] = [dict(date=pd.Timestamp('2024-06-03'), quantity=1., basis=1.)]
    assert e.order('fixture:US:0', 'sell', 1, {'raw_open': .1, 'halted': False}, pd.Timestamp('2024-06-04'), {}) == 0
    assert e.cash == 1 and e.quantity('fixture:US:0') == 1 and e.cost_basis('fixture:US:0') == 1


def test_complete_ca_coverage_and_valuation_required():
    bars, a = build_us(count=10)
    targets = pd.DataFrame(dict(date=pd.to_datetime(['2024-06-03']), instrument_id=['fixture:US:0'], target_weight=[1.]))
    coverage_start = a.corporate_actions.start
    a.corporate_actions.start = pd.Timestamp('2024-06-04')
    with pytest.raises(ContractError, match='COVERAGE_UNKNOWN'):
        ExecutionEngine(a).run(bars, targets, '2024-06-03', '2024-06-07')
    a.corporate_actions.start = coverage_start
    missing = bars.loc[~((bars.instrument_id == 'fixture:US:0') & (bars.date == pd.Timestamp('2024-06-05')))]
    with pytest.raises(ContractError, match='valuation missing'):
        ExecutionEngine(a).run(missing, targets, '2024-06-03', '2024-06-07')


def test_benchmark_currency_and_coverage_are_explicit():
    _, a = build_us(count=10)
    s = ResearchSpec('US', 'ALL_US_COMMON', '2023-09-01', '2025-02-28', 'SYNTHETIC_PRICE_INDEX', 'USD')
    b = benchmark(a, s)
    with pytest.raises(ContractError, match='currency'):
        validate_benchmark(b.assign(currency='CNY'), a.calendar, s.start_date, s.end_date, 'USD')
    with pytest.raises(ContractError, match='coverage'):
        validate_benchmark(b.iloc[1:], a.calendar, s.start_date, s.end_date, 'USD')


def test_oos_membership_does_not_depend_on_future_label(tmp_path):
    bars, a = build_us()
    s = ResearchSpec('US', 'ALL_US_COMMON', '2023-09-01', '2025-02-28', 'SYNTHETIC_PRICE_INDEX', 'USD',
                     model=('FactorScore',), train_months=3, validation_months=1, test_months=2)
    dataset = build_dataset(bars, a, s)
    key = (dataset.date == pd.Timestamp('2024-06-03')) & (dataset.instrument_id == 'fixture:US:10')
    assert key.sum() == 1
    dataset.loc[key, 'target'] = np.nan
    predictions, folds = run_models(dataset, s, a.calendar, tmp_path / 'predictions')
    f = predictions['FactorScore']
    row = f.loc[(f.date == pd.Timestamp('2024-06-03')) & (f.instrument_id == 'fixture:US:10')]
    assert len(row) == 1 and row.target.isna().all() and row.score.notna().all()
    assert sum(x['test_unlabeled_rows'] for x in folds) >= 1


@pytest.mark.parametrize('years', [10, 20])
def test_long_walkforward_actual_horizons_purge_embargo(years):
    c = us_calendar('2000-01-01', f'{2000 + years}-01-01')
    d = c.schedule.index
    f = pd.DataFrame({'date': d, 'entry_date': pd.Series(d).shift(-1), 'exit_date': pd.Series(d).shift(-6)})
    folds = list(ExpandingWalkForwardSplitter(initial_train_months=24, validation_months=3, test_months=6,
                                              step_months=6, purge_days=5, embargo_days=5).split(f, calendar=d))
    assert len(folds) >= (years - 3) * 2
    for fold in folds:
        assert fold.audit.train_max_exit_date < fold.audit.validation_first_date
        assert fold.audit.validation_max_exit_date < fold.audit.test_first_date
        assert len(fold.purged_dates) == len(fold.embargoed_dates) == 5
