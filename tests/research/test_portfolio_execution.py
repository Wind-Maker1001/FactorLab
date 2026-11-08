"""Synthetic multi-security target execution accounting contracts."""
import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig, OrderSizeRule
from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import SchemaError
from factorlab.portfolio.topk import TopKPortfolio
from tests.research.test_execution_backtest_phase3 import _bars, _benchmark


DAYS = pd.bdate_range('2024-01-02', periods=5)
SYMBOLS = ['000001.SZ', '000002.SZ', '600000.SH', '300001.SZ', '688001.SH']


def engine(cash=10000, **kwargs):
    return AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=cash, **kwargs),
                                    FeeSchedule.a_share_default(slippage_bps=0))


def targets(*snapshots):
    return pd.DataFrame([dict(date=DAYS[i], symbol=s, target_weight=w)
                         for i, weights in snapshots for s, w in weights.items()])


def reconcile(result, cash=10000):
    for row in result.trades.itertuples():
        cash += (row.trade_notional if row.side == 'sell' else -row.trade_notional) - row.transaction_cost
        assert row.cash_after == pytest.approx(cash)
    assert result.nav.iloc[-1].cash == pytest.approx(cash)
    for row in result.nav.itertuples():
        positions = result.positions[result.positions.date == row.date]
        assert row.portfolio_value == pytest.approx(row.cash + positions.market_value.sum())
        fills = result.trades[result.trades.execution_date == row.date]
        assert row.buy_notional == pytest.approx(fills.loc[fills.side == 'buy', 'trade_notional'].sum())
        assert row.sell_notional == pytest.approx(fills.loc[fills.side == 'sell', 'trade_notional'].sum())
        assert row.turnover == pytest.approx(max(row.buy_notional, row.sell_notional) / row.turnover_denominator)
    assert (result.trades.execution_date > result.trades.signal_date).all()


@pytest.mark.parametrize('n', [2, 3, 5])
def test_hand_calculable_multi_security(n):
    symbols = SYMBOLS[:n]
    t = targets((0, dict.fromkeys(symbols, .15)), (2, dict.fromkeys(symbols, 0)))
    bars = _bars(DAYS, symbols)
    result = engine().run_target_weights(t, bars, _benchmark(DAYS))
    buys = result.trades[result.trades.side == 'buy']
    for row in buys.itertuples():
        price = 10 + symbols.index(row.symbol)
        assert row.executed_shares == 100
        assert row.trade_notional == 100 * price
    assert result.nav.iloc[-1].holdings == 0
    assert result.nav.iloc[-1].portfolio_value == pytest.approx(10000 - result.trades.transaction_cost.sum())
    assert len(result.target_comparison) == 2*n
    reconcile(result)
    again = engine().run_target_weights(t, bars, _benchmark(DAYS))
    pd.testing.assert_frame_equal(result.nav, again.nav)
    pd.testing.assert_frame_equal(result.trades, again.trades)


def test_sell_success_buy_limit_and_old_universe_exit():
    a, b = SYMBOLS[:2]
    bars = _bars(DAYS, [a, b], overrides={(DAYS[2], b): {'raw_open': 12.1}})
    result = engine().run_target_weights(targets((0, {a:.5}), (1, {b:.5})), bars, _benchmark(DAYS))
    assert result.trades.iloc[-1].side == 'sell'
    assert result.rejections.iloc[-1].reason == 'buy_price_limit'
    assert result.nav.iloc[1].holdings == 0
    old = result.target_comparison.query('symbol == @a').iloc[-1]
    assert old.target_weight == old.actual_weight == 0
    reconcile(result)


@pytest.mark.parametrize('failure', ['suspended', 'missing_open', 'missing_bar', 'sell_price_limit'])
def test_blocked_old_holding_still_uses_capital(failure):
    a,b=SYMBOLS[:2]
    override = {'suspended': {'trade_status':0}, 'missing_open': {'raw_open':np.nan},
                'missing_bar': {}, 'sell_price_limit': {'raw_open':9}}[failure]
    bars = _bars(DAYS,[a,b],overrides={(DAYS[2],a):override})
    if failure == 'missing_bar':
        bars = bars[~((bars.date == DAYS[2]) & (bars.symbol == a))]
    result=engine().run_target_weights(targets((0,{a:.9}),(1,{b:1.})),bars,_benchmark(DAYS))
    assert failure in set(result.rejections.reason)
    assert 'cash_constraint' in set(result.rejections.reason)
    comparison = result.target_comparison
    old=comparison[(comparison.symbol==a) & (comparison.execution_date==DAYS[2])].iloc[0]
    assert old.target_weight == 0 and old.actual_weight > .8
    reconcile(result)


def test_zero_target_missing_open_and_nonzero_missing_price():
    a,b=SYMBOLS[:2]
    bars=_bars(DAYS,[a,b],overrides={(DAYS[2],a):{'raw_open':np.nan},(DAYS[2],b):{'raw_open':np.nan}})
    result=engine().run_target_weights(targets((0,{a:.5}),(1,{a:0,b:.5})),bars,_benchmark(DAYS))
    assert set(result.rejections.side) == {'sell','target'}
    assert result.rejections.loc[result.rejections.side=='target','requested_shares'].isna().all()
    assert result.target_comparison.iloc[-1].target_shares != result.target_comparison.iloc[-1].target_shares
    reconcile(result)


def test_fees_prevent_full_buy_and_expose_rounding_deviation():
    result=engine(1000).run_target_weights(targets((0,{SYMBOLS[0]:1})),_bars(DAYS,SYMBOLS[:1]),_benchmark(DAYS))
    assert result.trades.empty
    assert result.rejections.iloc[0].reason == 'cash_constraint'
    assert result.target_comparison.iloc[0].weight_deviation == -1


def test_star_minimum_and_increment_and_custom_rule():
    star=SYMBOLS[-1]
    bars=_bars(DAYS,[star])
    result=engine(2510).run_target_weights(targets((0,{star:1})),bars,_benchmark(DAYS))
    assert result.trades.iloc[0].executed_shares == 250
    assert result.rejections.iloc[0].reason == 'cash_constraint'
    result=engine(3510,target_order_sizes={star:OrderSizeRule(250,100)}).run_target_weights(
        targets((0,{star:1})),bars,_benchmark(DAYS))
    assert result.trades.iloc[0].executed_shares == 350


@pytest.mark.parametrize('weights', [[-.1,.5],[np.nan,.5],[.8,.8]])
def test_reject_invalid_weights(weights):
    with pytest.raises(SchemaError):
        engine().run_target_weights(targets((0,dict(zip(SYMBOLS,weights)))),_bars(DAYS,SYMBOLS),_benchmark(DAYS))


def test_date_and_duplicate_validation():
    t=targets((0,{SYMBOLS[0]:.5}))
    for bad in [pd.concat([t,t]), t.assign(date=DAYS[-1]), t.assign(date=DAYS[0]+pd.Timedelta(hours=12)),
                t.assign(date=pd.Timestamp('2024-01-06'))]:
        with pytest.raises(SchemaError):
            engine().run_target_weights(bad,_bars(DAYS,SYMBOLS),_benchmark(DAYS))


def test_topk_and_explicit_targets_match_execution():
    symbols=SYMBOLS[:2]
    scores=pd.DataFrame([dict(date=DAYS[i],symbol=s,score=j+i) for i in [0,2] for j,s in enumerate(symbols)])
    config=dict(top_k=2,minimum_cross_section=2,rebalance_days=2)
    selected=TopKPortfolio(**config).construct(scores,trading_dates=DAYS)
    # Equal ranks/symbol priority here; no cash contention changes the priority.
    old=engine(**config).run(scores,_bars(DAYS,symbols),_benchmark(DAYS))
    new=engine(**config).run_target_weights(selected,_bars(DAYS,symbols),_benchmark(DAYS))
    pd.testing.assert_frame_equal(old.nav,new.nav)
    pd.testing.assert_frame_equal(old.positions,new.positions)


def test_star_small_reduction_does_not_force_liquidation():
    star = SYMBOLS[-1]
    result = engine(10000).run_target_weights(targets((0, {star:.25}), (1, {star:.1})),
                                             _bars(DAYS, [star]), _benchmark(DAYS))
    assert len(result.trades) == 1
    assert result.positions.iloc[-1].shares == 250
    assert result.target_comparison.iloc[-1].weight_deviation > .1


def test_executor_rejects_same_day_sell():
    from factorlab.backtest.execution import PositionBook, _prepare_bars
    book = PositionBook()
    symbol = SYMBOLS[0]
    book.buy(symbol, DAYS[1], 100)
    e = engine()
    bars = _prepare_bars(_bars(DAYS, [symbol]), e.config)
    trades, rejections = [], []
    fill = e._execute_order(book=book, day_bars=bars[bars.date == DAYS[1]].set_index('symbol'),
                            trade_rows=trades, rejection_rows=rejections, signal_date=DAYS[0],
                            execution_date=DAYS[1], symbol=symbol, side='sell', requested_shares=100,
                            cash=0., metadata=None)
    assert fill is None and not trades
    assert rejections[0]['reason'] == 't_plus_one'
    assert book.total_shares(symbol) == 100


def test_future_closes_do_not_size_open_orders():
    t = targets((0, {SYMBOLS[0]:.5}))
    bars = _bars(DAYS, SYMBOLS[:1])
    before = engine().run_target_weights(t, bars, _benchmark(DAYS))
    bars.loc[bars.date >= DAYS[1], 'raw_close'] = 1000.
    after = engine().run_target_weights(t, bars, _benchmark(DAYS))
    pd.testing.assert_frame_equal(before.trades, after.trades)
    assert before.nav.iloc[0].strategy_nav != after.nav.iloc[0].strategy_nav


def test_optimizer_end_to_end():
    from examples.research.portfolio_execution_synthetic_demo import run_demo
    result, summary = run_demo()
    assert summary['target_weights'][SYMBOLS[0]] == pytest.approx(.64)
    assert summary['actual_shares'] == {'000001.SZ': 600, '600000.SH': 100}
    reconcile(result)


def test_optimizer_turnover_remains_distinct():
    t=targets((0,{SYMBOLS[0]:.5})).assign(optimizer_turnover=1.2)
    result=engine().run_target_weights(t,_bars(DAYS,SYMBOLS[:1]),_benchmark(DAYS))
    assert result.nav.iloc[0].optimizer_turnover == 1.2
    assert result.nav.iloc[0].turnover == .5
