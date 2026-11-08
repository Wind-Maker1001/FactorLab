"""Hand-calculated actual-holdings feedback contracts."""
from dataclasses import FrozenInstanceError, replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import SchemaError
from factorlab.portfolio.feedback import ExecutionFeedback, SaleRestriction
from factorlab.portfolio.optimizer import OptimizationError, OptimizationInput, OptimizerConfig, PortfolioOptimizer
from tests.research.test_execution_backtest_phase3 import _bars, _benchmark

DAYS = pd.bdate_range('2024-01-02', periods=5)
A, B = '000001.SZ', '000002.SZ'


def engine(cash=10000., fee=0.):
    return AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=cash),
        FeeSchedule([FeeRule(date(2000, 1, 1), commission_bps=0,
            minimum_commission_cny=fee, slippage_bps=0)]))


def session(cash=10000., fee=0., bars=None):
    return engine(cash, fee).start_session(_bars(DAYS, [A, B]) if bars is None else bars,
                                          _benchmark(DAYS), signal_date=DAYS[0])


def data(s, symbols=(A, B), scores=(1., 1.)):
    return OptimizationInput(symbols, s.snapshot.as_of,
        covariance=pd.DataFrame(np.eye(len(symbols)), index=symbols, columns=symbols),
        covariance_as_of=s.snapshot.as_of,
        scores=pd.Series(scores, index=symbols))


def feedback(s, investment=.5, limit=.5, objective='score', **kwargs):
    return ExecutionFeedback(s, PortfolioOptimizer(OptimizerConfig(objective=objective,
        investment=investment, turnover_limit=limit, **kwargs)))


def assert_reconciled(result, initial=10000.):
    cash = initial
    for row in result.trades.itertuples():
        cash += (1 if row.side == 'sell' else -1)*row.trade_notional - row.transaction_cost
        assert row.cash_after == pytest.approx(cash)
    for row in result.nav.itertuples():
        positions = result.positions[result.positions.date == row.date]
        assert row.portfolio_value == pytest.approx(row.cash + positions.market_value.sum())
        assert row.strategy_nav == pytest.approx(row.portfolio_value/initial)
        fills = result.trades[result.trades.execution_date == row.date]
        gross = fills.trade_notional.sum()
        assert row.gross_security_turnover == pytest.approx(gross/row.turnover_denominator)
    assert result.nav.iloc[-1].cash == pytest.approx(cash)


def test_empty_then_two_decisions_use_actual_not_target():
    s = session()
    initial = s.snapshot
    assert initial.cash == initial.nav == 10000
    assert initial.cash_weight == 1 and initial.security_weights.empty
    f = feedback(s)
    first = f.optimize(data(s))
    assert first.optimization.turnover == pytest.approx(.5)
    assert dict(first.optimization.target_weights) == {A:.25, B:.25}
    assert s.snapshot.pending_targets == ((A, .25), (B, .25))
    assert not s.snapshot.holdings and not s.snapshot.last_fills
    snap = f.advance()
    # 2500/10 -> 200 shares; 2500/11 -> 200 shares.
    assert [h.shares for h in snap.holdings] == [200, 200]
    assert snap.cash == 5800 and snap.nav == 10000
    assert snap.security_weights.to_dict() == {A:.2, B:.22}
    assert snap.last_targets == ((A,.25), (B,.25))
    assert all(h.sellable_shares == 0 for h in snap.holdings)
    assert s.sellable_next_open(A) == 200
    second = f.optimize(data(s, scores=(0., 1.)))
    assert second.optimization.parameters['inputs']['previous_weights'] == {A:.2, B:.22}
    assert second.optimization.turnover == pytest.approx(.48)
    assert second.snapshot == snap
    f.advance()
    assert s.snapshot.security_weights.to_dict() == {B:.44}
    assert s.snapshot.cash == 5600
    assert s.result.nav.iloc[-1].gross_security_turnover == pytest.approx(.42)
    assert_reconciled(s.result)
    assert initial.cash == 10000 and not initial.holdings


def test_partial_cash_fill_and_next_turnover_uses_fill():
    s = session(cash=10000, fee=5)
    f = feedback(s, investment=1, limit=1)
    first = f.optimize(data(s, symbols=(A,), scores=(1.,)))
    snap = f.advance()
    assert snap.holdings[0].shares == 900
    assert snap.cash == 995 and snap.nav == 9995
    assert snap.last_rejections[0]['reason'] == 'cash_constraint'
    assert snap.last_rejections[0]['rejected_shares'] == 100
    assert snap.last_fills[0]['executed_shares'] == 900
    assert snap.last_fills[0]['reported_at'] == DAYS[1] + pd.Timedelta(hours=9, minutes=30)
    assert first.optimization.target_weights[A] == 1
    next_decision = f.optimize(data(s, symbols=(A,), scores=(1.,)))
    assert next_decision.optimization.turnover == pytest.approx(995/9995)
    assert next_decision.optimization.parameters['inputs']['previous_weights'][A] == pytest.approx(9000/9995)
    assert_reconciled(s.result)


def test_rejected_first_buy_stays_all_cash():
    s = session(cash=1000, fee=5)
    f = feedback(s, investment=1, limit=1)
    f.optimize(data(s, symbols=(A,), scores=(1.,)))
    snap = f.advance()
    assert snap.cash == snap.nav == 1000 and not snap.holdings
    assert snap.last_rejections[0]['reason'] == 'cash_constraint'
    assert s.result.target_comparison.iloc[0].weight_deviation == -1
    assert feedback(s, investment=1, limit=1).optimize(data(s, symbols=(A,), scores=(1.,))).optimization.turnover == 1


def test_unforeseen_suspension_and_cash_rejection_are_not_relaxed():
    bars = _bars(DAYS, [A, B], overrides={(DAYS[2], A): {'trade_status':0}})
    s = session(bars=bars)
    feedback(s, investment=.9, limit=.9).optimize(data(s, symbols=(A,), scores=(1.,)))
    s.advance()
    decision = feedback(s, investment=1, limit=1.9).optimize(data(s, symbols=(B,), scores=(1.,)))
    assert decision.optimization.turnover == pytest.approx(1.9)
    snap = s.advance()
    assert [h.shares for h in snap.holdings] == [900]
    assert snap.cash == 1000 and snap.nav == 10000
    assert {r['reason'] for r in snap.last_rejections} == {'suspended', 'cash_constraint'}
    assert s.result.nav.iloc[-1].gross_security_turnover == 0
    deviation = s.result.target_comparison.query('execution_date == @DAYS[2]')
    assert dict(zip(deviation.symbol, deviation.weight_deviation)) == {A:.9, B:-1.}
    assert_reconciled(s.result)


@pytest.mark.parametrize('limit, succeeds', [(.5, True), (.499, False)])
def test_turnover_boundary_and_infeasibility(limit, succeeds):
    s = session()
    f = feedback(s, limit=limit)
    if succeeds:
        assert f.optimize(data(s)).optimization.turnover == pytest.approx(.5)
    else:
        with pytest.raises(OptimizationError) as exc:
            f.optimize(data(s))
        assert exc.value.status == 'infeasible'
        assert not s.snapshot.pending_targets and not s.snapshot.holdings


@pytest.mark.parametrize('objective', ['score', 'minimum_variance'])
def test_known_locked_holding_is_solver_floor(objective):
    s = session()
    feedback(s, investment=.5, limit=.5).optimize(data(s, symbols=(A,), scores=(1.,)))
    s.advance()
    restriction = SaleRestriction(A, 500, s.snapshot.as_of, DAYS[3], 'explicit dated sale prohibition')
    f = feedback(s, investment=.5, limit=1, objective=objective)
    if objective == 'score':
        with pytest.raises(OptimizationError) as exc:
            f.optimize(data(s, scores=(0., 1.)), sale_restrictions=[restriction])
        assert exc.value.status == 'infeasible'
    else:
        result = f.optimize(data(s), sale_restrictions=[restriction]).optimization
        assert result.target_weights[A] == pytest.approx(.5)
        assert result.target_weights[B] == pytest.approx(0, abs=1e-8)


@pytest.mark.parametrize('locked', [450, 500])
def test_known_share_lock_survives_next_open_price_drift(locked):
    bars = _bars(DAYS, [A,B], overrides={(DAYS[2], A): {'raw_open':10.5, 'raw_close':10.5}})
    s = session(bars=bars)
    s.submit_targets(pd.Series({A:.5}))
    s.advance()
    restriction = SaleRestriction(A, locked, s.snapshot.as_of, DAYS[2], 'known share lock')
    feedback(s).optimize(data(s, scores=(1.,0.)), sale_restrictions=[restriction])
    snap = s.advance()
    assert snap.holdings[0].shares == 500 and snap.cash == 5000 and snap.nav == 10250
    assert snap.last_rejections[0]['reason'] == 'sale_restriction'
    assert snap.last_rejections[0]['rejected_shares'] == 100
    assert not snap.last_fills
    assert s.result.target_comparison.iloc[-2].weight_deviation == pytest.approx(5250/10250-.5)
    assert_reconciled(s.result)


def test_locked_outside_universe_is_infeasible():
    s = session()
    s.submit_targets(pd.Series({A:.5}))
    s.advance()
    r = SaleRestriction(A, 500, s.snapshot.as_of, DAYS[3], 'known prohibition')
    with pytest.raises(OptimizationError, match='outside target universe'):
        feedback(s).optimize(data(s, symbols=(B,), scores=(1.,)), sale_restrictions=[r])
    assert not s.snapshot.pending_targets


def test_future_prices_cannot_change_prior_decision_or_snapshot():
    bars = _bars(DAYS, [A, B])
    changed = bars.copy()
    changed.loc[(changed.date >= DAYS[1]) & (changed.symbol == A), ['raw_open', 'raw_close']] = 10.5
    outcomes = []
    for source in [bars, changed]:
        s = session(bars=source)
        d = feedback(s, investment=.5, limit=.5).optimize(data(s, symbols=(A,), scores=(1.,)))
        before = s.snapshot
        s.advance()
        outcomes.append((d, before, s.snapshot))
    assert outcomes[0][0].optimization.input_identity == outcomes[1][0].optimization.input_identity
    assert outcomes[0][1] == outcomes[1][1]
    assert outcomes[0][2].holdings[0].shares == 500
    assert outcomes[1][2].holdings[0].shares == 400
    assert outcomes[0][0].execution_time > outcomes[0][0].decision_time


def test_snapshot_dates_stale_marks_and_no_order_day():
    bars = _bars(DAYS, [A, B], overrides={(DAYS[2], A): {'raw_close':np.nan}})
    s = session(bars=bars)
    s.submit_targets(pd.Series({A:.5}))
    first = s.advance()
    second = s.advance()
    assert second.holdings[0].mark_time == first.as_of
    assert second.holdings[0].sellable_shares == 500
    assert not second.last_fills and not second.last_rejections
    assert second.cash == 5000 and second.nav == 10000
    with pytest.raises(FrozenInstanceError):
        first.cash = 0
    with pytest.raises(TypeError):
        first.last_fills[0]['executed_shares'] = 0


@pytest.mark.parametrize('bad', ['decision', 'signal', 'risk', 'previous', 'restriction'])
def test_future_or_conflicting_input_rejected_without_pending_order(bad):
    s = session()
    d = data(s)
    kwargs = {}
    if bad == 'decision':
        d = replace(d, decision_time=DAYS[1])
    elif bad == 'signal':
        kwargs['signal_time'] = DAYS[1]
    elif bad == 'risk':
        d = replace(d, covariance_as_of=DAYS[1])
    elif bad == 'previous':
        d = replace(d, previous_weights=pd.Series({A:.5}))
    else:
        s.submit_targets(pd.Series({A:.5}))
        s.advance()
        d = data(s)
        kwargs['sale_restrictions'] = [SaleRestriction(A, 500, DAYS[2], DAYS[3], 'future evidence')]
    with pytest.raises((SchemaError, OptimizationError)):
        feedback(s).optimize(d, **kwargs)
    assert not s.snapshot.pending_targets


def test_session_and_legacy_batch_exact_execution_parity():
    bars = _bars(DAYS, [A, B])
    e = engine(fee=5)
    s = e.start_session(bars, _benchmark(DAYS), signal_date=DAYS[0])
    weights = [{A:.5, B:.2}, {B:.6}]
    for w in weights:
        s.submit_targets(pd.Series(w))
        s.advance()
    s.advance()
    s.advance()
    targets = pd.DataFrame([dict(date=DAYS[i], symbol=k, target_weight=v)
                            for i, w in enumerate(weights) for k, v in w.items()])
    batch = e.run_target_weights(targets, bars, _benchmark(DAYS))
    live = s.result
    pd.testing.assert_frame_equal(live.nav.drop(columns='gross_security_turnover'), batch.nav)
    for name in ['trades', 'positions', 'rejections', 'signals', 'target_comparison']:
        pd.testing.assert_frame_equal(getattr(live, name), getattr(batch, name))
    assert_reconciled(live)
    with pytest.raises(SchemaError, match='no next'):
        s.advance()


def test_future_fill_does_not_change_real_holdings_second_decision():
    normal = _bars(DAYS, [A, B])
    changed = normal.copy()
    changed.loc[(changed.date >= DAYS[2]) & (changed.symbol == B), ['raw_open', 'raw_close']] = 11.5
    results = []
    for bars in [normal, changed]:
        s = session(bars=bars)
        s.submit_targets(pd.Series({A:.5}))
        s.advance()
        decision = feedback(s, investment=.5, limit=1).optimize(data(s, scores=(0.,1.)))
        s.advance()
        results.append((decision, s.snapshot))
    before_a, before_b = results[0][0].snapshot, results[1][0].snapshot
    assert replace(before_a, last_fills=()) == replace(before_b, last_fills=())
    pd.testing.assert_frame_equal(pd.DataFrame(before_a.last_fills), pd.DataFrame(before_b.last_fills))
    assert results[0][0].optimization.input_identity == results[1][0].optimization.input_identity
    assert results[0][1].holdings[0].shares == results[1][1].holdings[0].shares == 400
    assert results[0][1].cash == 5600
    assert results[1][1].cash == 5400


@pytest.mark.parametrize('field', ['decision', 'risk', 'signal', 'returns'])
def test_mixed_timezone_inputs_do_not_bypass_close_boundary(field):
    s = session()
    d = data(s)
    aware_future = (s.snapshot.as_of + pd.Timedelta(hours=1)).tz_localize('Asia/Shanghai')
    kwargs = {}
    if field == 'decision':
        d = replace(d, decision_time=aware_future)
    elif field == 'risk':
        d = replace(d, covariance_as_of=aware_future)
    elif field == 'signal':
        kwargs['signal_time'] = aware_future
    else:
        d = replace(d, covariance=None, covariance_as_of=None,
                    returns=pd.DataFrame([[0.,0.], [.1,.1]], columns=(A,B),
                        index=pd.date_range('2024-01-01', periods=2, tz='Asia/Shanghai')))
    with pytest.raises(SchemaError, match='exchange-local'):
        feedback(s).optimize(d, **kwargs)
    assert not s.snapshot.pending_targets


@pytest.mark.parametrize('weights', [pd.Series([.1,.2], index=[A,A]),
                                     pd.Series({A:np.nan}), pd.Series({A:-.1})])
def test_invalid_floors_are_not_hidden_by_actual_holdings_merge(weights):
    s = session()
    with pytest.raises((SchemaError, OptimizationError)):
        feedback(s).optimize(replace(data(s), minimum_weights=weights))
    assert not s.snapshot.pending_targets


def test_pending_duplicate_and_unknown_actions_block():
    s = session()
    blocked = ExecutionFeedback(s, PortfolioOptimizer(), require_verified_actions=True)
    with pytest.raises(SchemaError, match='UNKNOWN'):
        blocked.optimize(data(s))
    s.submit_targets(pd.Series({A:.5}))
    with pytest.raises(SchemaError, match='already pending'):
        s.submit_targets(pd.Series({B:.5}))
    assert s.snapshot.pending_targets == ((A,.5),)
