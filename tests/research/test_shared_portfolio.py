"""Synthetic execution, causal signals and strict no-fill valuation boundaries."""
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig, PositionBook
from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.strategies.real_shared_portfolio import (
    InputBlocked, audit_ledger, evaluate, execute, monthly_targets, validate_panel,
)

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = ['000001.SZ', '601857.SH']


def fixture():
    config = json.loads((ROOT / 'configs/research/p8-01-shared-portfolio.json').read_text())
    config['initial_cash'] = 10010
    dates = pd.bdate_range('2016-01-04', periods=25)
    history_dates = pd.bdate_range(end=dates[-1], periods=55)
    history = pd.DataFrame([dict(date=d, symbol=s, raw_open=10., raw_high=10., raw_low=10., raw_close=10.,
        volume=100000., trade_status=1, is_st=0) for d in history_dates for s in SYMBOLS])
    reference = history.loc[history.symbol.eq('601857.SH') & history.date.isin(dates)].copy()
    return config, dates, history, reference


def test_shared_cash_order_lots_fees_and_daily_valuation():
    config, dates, history, reference = fixture()
    result, targets, _, audit = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    first = result.trades.loc[result.trades.execution_date.eq(dates[1])]
    assert first.symbol.tolist() == SYMBOLS
    assert first.executed_shares.tolist() == [500, 400]
    assert 'cash_constraint' in set(result.rejections.reason)
    assert (result.trades.executed_shares % 100 == 0).all()
    assert result.nav.iloc[0].cash == pytest.approx(config['initial_cash'] - first.trade_notional.sum() - first.transaction_cost.sum())
    assert audit['status'] == 'PASSED'
    assert targets.groupby('date').target_weight.sum().eq(1).all()
    assert (result.trades.execution_date > result.trades.signal_date).all()
    nav, evaluation, metrics = evaluate(result, dates, config, 'equal_weight', source_kind='SYNTHETIC')
    assert len(nav) == len(dates)
    assert nav.iloc[0].cash == config['initial_cash']
    assert metrics['market_index_status'] == 'UNAVAILABLE'
    assert metrics['total_return'] is None
    assert metrics['total_return_status'] == 'TOTAL_RETURN_UNAVAILABLE'
    assert evaluation.context.benchmark_id == 'MODEL_CASH_ZERO_RETURN_NOT_INDEX'
    assert metrics['fees']['transaction_cost'] == pytest.approx(result.trades.transaction_cost.sum())


@pytest.mark.parametrize('corruption', ['missing_day', 'missing_close', 'missing_open', 'duplicate', 'invalid_ohlc'])
def test_missing_prices_cannot_become_stale_marks(corruption):
    config, dates, history, reference = fixture()
    key = history.date.eq(dates[5]) & history.symbol.eq(SYMBOLS[0])
    if corruption == 'missing_day':
        history = history.loc[~key]
    elif corruption == 'duplicate':
        history = pd.concat([history, history.loc[key]])
    else:
        history.loc[key, {'missing_close': 'raw_close', 'missing_open': 'raw_open', 'invalid_ohlc': 'raw_high'}[corruption]] = 9. if corruption == 'invalid_ohlc' else np.nan
    with pytest.raises(InputBlocked):
        execute(history, dates, SYMBOLS, reference, 'equal_weight', config)


def test_missing_execution_reference_blocks_without_synthetic_index():
    config, dates, history, reference = fixture()
    with pytest.raises(InputBlocked):
        execute(history, dates, SYMBOLS, reference.iloc[1:], 'equal_weight', config)


@pytest.mark.parametrize('field', ['cash', 'portfolio_value'])
def test_ledger_audit_detects_cash_or_equity_corruption(field):
    config, dates, history, reference = fixture()
    result, _, _, _ = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    nav = result.nav.copy()
    nav.loc[0, field] += 1
    with pytest.raises(SchemaError):
        audit_ledger(replace(result, nav=nav), history, config['initial_cash'])


def test_ledger_audit_detects_share_corruption():
    config, dates, history, reference = fixture()
    result, _, _, _ = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    positions = result.positions.copy()
    positions.loc[0, 'shares'] += 100
    with pytest.raises(SchemaError):
        audit_ledger(replace(result, positions=positions), history, config['initial_cash'])


def test_momentum_is_causal_and_unallocated_capital_stays_cash():
    _, dates, history, _ = fixture()
    a, _ = monthly_targets(history, dates, SYMBOLS, 'momentum')
    changed = history.copy()
    changed.loc[changed.date > dates[0], 'raw_close'] *= 100
    b, _ = monthly_targets(changed, dates, SYMBOLS, 'momentum')
    pd.testing.assert_frame_equal(a.loc[a.date.eq(dates[0])], b.loc[b.date.eq(dates[0])])
    first = a.loc[a.date.eq(dates[0])]
    assert first.target_weight.tolist() == [.2, .2]
    config, dates, history, reference = fixture()
    result, _, _, _ = execute(history, dates, SYMBOLS, reference, 'momentum', config)
    first_cost = result.trades.loc[result.trades.execution_date.eq(dates[1]), 'transaction_cost'].sum()
    assert result.nav.iloc[0].cash >= .6 * config['initial_cash'] - first_cost


def test_momentum_top_five_rank_and_lexical_ties():
    _, dates, history, _ = fixture()
    source = history.loc[history.symbol.eq(SYMBOLS[0])]
    symbols = [f'{i:06d}.SZ' for i in range(1, 8)]
    panel = pd.concat([source.assign(symbol=s) for s in reversed(symbols)])
    targets, _ = monthly_targets(panel, dates, symbols, 'momentum')
    first = targets.loc[targets.date.eq(dates[0])]
    assert first.loc[first.target_weight.gt(0), 'symbol'].tolist() == symbols[:5]
    assert first.target_weight.sum() == pytest.approx(1.)


def test_insufficient_warmup_emits_real_zero_snapshot():
    config, dates, history, reference = fixture()
    history = history.loc[history.date.isin(dates)]
    result, targets, decisions, audit = execute(history, dates, SYMBOLS, reference, 'momentum', config)
    assert decisions.iloc[0].reason == 'INSUFFICIENT_HISTORY_CASH'
    assert targets.loc[targets.date.eq(dates[0]), 'target_weight'].eq(0).all()
    assert result.nav.iloc[0].cash == config['initial_cash']
    assert audit['status'] == 'PASSED'


def test_terminal_signal_is_recorded_but_not_submitted():
    _, _, history, _ = fixture()
    dates = pd.DatetimeIndex(['2016-01-29', '2016-02-01'])
    targets, decisions = monthly_targets(history, dates, SYMBOLS, 'momentum')
    assert not targets.date.eq(dates[-1]).any()
    assert decisions.iloc[-1].reason == 'NO_NEXT_OBSERVED_OPEN'


def test_next_open_sizes_orders_not_signal_close():
    config, dates, history, reference = fixture()
    mask = history.date.ge(dates[1]) & history.symbol.eq(SYMBOLS[0])
    history.loc[mask, ['raw_open', 'raw_close', 'raw_high', 'raw_low']] = 10.5
    result, _, _, _ = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    first = result.trades.iloc[0]
    assert first.execution_date == dates[1]
    assert first.execution_price == 10.5
    assert first.executed_shares == 400


@pytest.mark.parametrize('mode,reason', [('suspended', 'suspended'), ('limit', 'buy_price_limit'), ('st_limit', 'buy_price_limit')])
def test_suspension_and_price_limit_rejections_have_no_retry(mode, reason):
    config, dates, history, reference = fixture()
    mask = history.date.eq(dates[1]) & history.symbol.eq(SYMBOLS[0])
    if mode == 'suspended':
        history.loc[mask, ['trade_status', 'volume']] = [0, 0]
    else:
        history.loc[mask, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = 10.5 if mode == 'st_limit' else 11.
        if mode == 'st_limit':
            history.loc[history.symbol.eq(SYMBOLS[0]), 'is_st'] = 1
    result, _, _, _ = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    assert reason in set(result.rejections.reason)
    assert result.trades.loc[result.trades.symbol.eq(SYMBOLS[0]) & result.trades.execution_date.lt(pd.Timestamp('2016-02-01'))].empty


def test_t_plus_one_book_prevents_same_day_sale():
    book = PositionBook()
    day = pd.Timestamp('2016-01-04')
    book.buy(SYMBOLS[0], day, 100)
    assert book.sellable_shares(SYMBOLS[0], day) == 0
    assert book.sellable_shares(SYMBOLS[0], day + pd.Timedelta(days=1)) == 100
    with pytest.raises(ConfigurationError, match='non-sellable'):
        book.sell(SYMBOLS[0], day, 100)


def test_weight_sum_is_rejected_by_existing_engine():
    config, dates, history, reference = fixture()
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(benchmark_open_price_column='raw_open', benchmark_close_price_column='raw_close'), FeeSchedule.a_share_default())
    targets = pd.DataFrame(dict(date=[dates[0]] * 2, symbol=SYMBOLS, target_weight=[.6, .6]))
    with pytest.raises(SchemaError, match='sum'):
        engine.run_target_weights(targets, history, reference)


def test_publication_excludes_private_fields_and_rejects_artifact_drift(tmp_path):
    import importlib.util
    from factorlab.dataset.identity import file_hash
    spec = importlib.util.spec_from_file_location('shared_publisher', ROOT / 'tools/research/publish_shared_portfolio.py')
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    config, dates, history, reference = fixture()
    result, targets, decisions, audit = execute(history, dates, SYMBOLS, reference, 'equal_weight', config)
    nav, _, metrics = evaluate(result, dates, config, 'equal_weight', source_kind='SYNTHETIC')
    private = tmp_path / 'private'
    folder = private / '2016' / 'equal_weight'
    folder.mkdir(parents=True)
    for name, frame in dict(nav=nav, trades=result.trades, rejections=result.rejections,
                            decisions=decisions, target_comparison=result.target_comparison).items():
        frame.to_csv(folder / (name + '.csv'), index=False)
    summary = dict.fromkeys(publisher.SUMMARY_FIELDS)
    summary.update(status='SYNTHETIC', years={'2016': dict(status='COMPLETED', schemes={'equal_weight': dict(metrics=metrics)})},
        artifact_sha256={str(p.relative_to(private)).replace('\\', '/'): file_hash(p) for p in private.rglob('*.csv')},
        secret_runtime_path='PRIVATE_DO_NOT_PUBLISH')
    (private / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    output = tmp_path / 'public'
    published = publisher.publish(private, output)
    assert 'secret_runtime_path' not in published
    assert 'PRIVATE_DO_NOT_PUBLISH' not in (output / 'real-summary.json').read_text()
    public_nav = pd.read_csv(output / 'daily-nav.csv')
    assert not {'cash', 'symbol', 'shares', 'mark_price', 'raw_close', 'execution_price'} & set(public_nav.columns)
    assert set(p.name for p in output.iterdir()) == {'annual-metrics.csv', 'daily-nav.csv', 'real-summary.json'}
    (folder / 'trades.csv').write_text('altered private receipt')
    with pytest.raises(ValueError, match='identity drift'):
        publisher.publish(private, tmp_path / 'drifted')
