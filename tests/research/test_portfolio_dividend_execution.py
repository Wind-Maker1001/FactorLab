"""SYNTHETIC / ENGINEERING_ONLY / NOT_FINANCIAL_EVIDENCE; NO LIVE MONEY.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
"""
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import DataQualityError, SchemaError
from tests.research.market_data_fixture import event
from tests.research.test_dividend_execution import artifact, plan_for, fees as zero_fees, KNOWN

# Reserved fictional fixture identities, not claims about listed instruments.
SYMBOLS = ('990001.SH', '990002.SH', '990003.SH')
LABELS = ['SYNTHETIC', 'ENGINEERING_ONLY', 'NOT_FINANCIAL_EVIDENCE', 'NO LIVE MONEY',
          'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


def scenario(root, *, empty=False, same_day=False):
    calendar = pd.bdate_range('2024-01-02', periods=13)
    days = calendar[1:]
    frames = []
    for index, symbol in enumerate(SYMBOLS):
        prices = np.full(13, 100.0)
        if index == 0:
            prices[4:] = 90.0
        elif index == 1:
            prices[6:] = 90.0
            prices[7:] = 88.0
        frames.append(pd.DataFrame(dict(date=calendar, symbol=symbol,
            raw_open=prices, raw_close=prices, raw_high=prices+1, raw_low=prices-1,
            volume=100000, trade_status=1, is_st=0,
            limit_up=prices*1.1, limit_down=prices*.9)))
    bars = pd.concat(frames, ignore_index=True)
    benchmark = pd.DataFrame(dict(date=calendar, adj_open=100., adj_close=100.))
    snapshots = {0: [.5, .49, 0], 3: [0, 1., 0], 6: [0, 1., 0],
                 8: [0, 1., 0], 10: [0, .5, .49], 11: [0, .6, .4]}
    targets = pd.DataFrame([dict(date=calendar[offset], symbol=symbol, target_weight=weight)
        for offset, weights in snapshots.items() for symbol, weight in zip(SYMBOLS, weights)])
    source = artifact(root, 'terms.json', {'scope': LABELS})
    specs = [(0, 'fixture-a', 1, 3, 7, '10'),
             (1, 'fixture-b', 3, 5, 9 if not same_day else 7, '10'),
             (1, 'fixture-b2', 4, 6, 7, '2'),
             (2, 'fixture-no-position', 1, 2, 7, '3')]
    plans = {}
    for index, symbol in enumerate(SYMBOLS):
        events = [event(source, security_id=symbol, event_id=identity, revision=2,
                    available_at=KNOWN, record_date=str(days[record].date()),
                    day=str(days[ex].date()), pay_date=str(days[pay].date()),
                    cash=Decimal(cash))
                  for security, identity, record, ex, pay, cash in specs
                  if security == index and not empty]
        plans[symbol] = plan_for(root / symbol, days, events=events, symbol=symbol)
    config = ExecutionBacktestConfig(initial_cash=100000)
    fees = FeeSchedule.a_share_default(slippage_bps=5)
    return days, bars, targets, benchmark, config, fees, plans


def execute(inputs, plans=True):
    days, bars, targets, benchmark, config, fees, dividend_plans = inputs
    return AShareExecutionBacktester(config, fees).run_target_weights(
        targets, bars, benchmark, dividend_plans=dividend_plans if plans else None)


def reconcile(result, config, fees):
    nav, trades = result.nav, result.trades
    dates = pd.DatetimeIndex(nav.date)
    flows = (trades.trade_notional.where(trades.side.eq('sell'), -trades.trade_notional)
             - trades.transaction_cost).groupby(trades.execution_date).sum().reindex(dates, fill_value=0)
    cash = nav.available_cash - config.initial_cash - (flows.to_numpy()+nav.dividend_cash_flow).cumsum()
    np.testing.assert_allclose(cash, 0, atol=1e-8, rtol=0)
    np.testing.assert_allclose(nav.trade_cash_flow, flows, atol=1e-8, rtol=0)
    position_residual = 0
    for symbol in SYMBOLS:
        fills = trades[trades.symbol.eq(symbol)]
        shares = fills.executed_shares.where(fills.side.eq('buy'), -fills.executed_shares)
        expected = shares.groupby(fills.execution_date).sum().reindex(dates, fill_value=0).cumsum()
        observed = result.positions[result.positions.symbol.eq(symbol)].set_index('date').shares.reindex(dates, fill_value=0)
        np.testing.assert_array_equal(observed, expected)
        position_residual = max(position_residual, int(abs(observed-expected).max()))
    for fill in trades.itertuples():
        assert abs(fees.calculate(fill.execution_date, fill.side, fill.trade_notional).total-fill.transaction_cost) < 1e-8
    np.testing.assert_allclose(trades[['commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage']].sum(axis=1),
                               trades.transaction_cost, atol=1e-8, rtol=0)
    entitlements = result.entitlements.set_index('event_id')
    for entitlement in result.entitlements.itertuples():
        position = result.positions[(result.positions.date == entitlement.record_date)
                                    & (result.positions.symbol == entitlement.security_id)]
        assert entitlement.eligible_shares == int(position.shares.sum())
    assert not result.entitlements.event_id.duplicated().any()
    assert not result.payments.event_id.duplicated().any()
    assert not result.recognitions.event_id.duplicated().any()
    for recognition in result.recognitions.itertuples():
        assert recognition.gross == entitlements.loc[recognition.event_id, 'gross']
        assert recognition.recognized == recognition.net == recognition.gross
    event_receivables = []
    for identity in entitlements.index:
        recognition = result.recognitions.set_index('event_id').loc[identity]
        payment = result.payments.set_index('event_id').loc[identity]
        outstanding = pd.Series([float(recognition.recognized)
            if recognition.effective_date <= day < payment.observed_on else 0.0 for day in dates])
        assert (outstanding >= 0).all()
        event_receivables.append(outstanding)
    np.testing.assert_allclose(nav.recognized_receivables, sum(event_receivables), atol=1e-8, rtol=0)
    for payment in result.payments.itertuples():
        entitlement = entitlements.loc[payment.event_id]
        assert payment.gross == entitlement.gross == entitlement.eligible_shares*entitlement.cash_per_share
        assert payment.net == payment.gross  # Frozen explicit zero tax fixture.
        assert payment.available_cash_delta == payment.net
    recognized = result.recognitions.groupby('effective_date').recognized.sum().reindex(dates, fill_value=0).astype(float)
    paid = result.payments.groupby('observed_on').net.sum().reindex(dates, fill_value=0).astype(float)
    np.testing.assert_allclose(nav.recognized_receivables, (recognized-paid).cumsum(), atol=1e-8, rtol=0)
    np.testing.assert_allclose(nav.dividend_cash_flow, paid, atol=1e-8, rtol=0)
    values = result.positions.groupby('date').market_value.sum().reindex(dates, fill_value=0)
    equity = nav.portfolio_value - nav.available_cash - values.to_numpy() - nav.recognized_receivables - nav.restricted_cash
    np.testing.assert_allclose(equity, 0, atol=1e-8, rtol=0)
    np.testing.assert_allclose(nav.strategy_nav, nav.portfolio_value/config.initial_cash, atol=1e-12, rtol=0)
    for name in ('cash_reconciliation', 'dividend_reconciliation', 'equity_reconciliation'):
        np.testing.assert_allclose(result.return_accounting[name], 0, atol=1e-8, rtol=0)
    return dict(cash_max_abs=float(abs(cash).max()), position_max_abs=position_residual,
                equity_max_abs=float(abs(equity).max()), receivables_reconciled=True,
                payment_amounts_reconciled=True, fees_reconciled=True)


@pytest.mark.parametrize('second_dividend', [False, True])
@pytest.mark.parametrize('sell_on_ex', [False, True])
@pytest.mark.parametrize('pay_on_ex', [False, True])
def test_ex_open_equity_and_unpaid_liquidity(tmp_path, second_dividend, sell_on_ex, pay_on_ex):
    calendar = pd.bdate_range('2024-01-02', periods=7)
    days = calendar[1:]
    ex, pay = days[2], days[2] if pay_on_ex else days[4]
    symbols = SYMBOLS[:2]
    source = artifact(tmp_path, 'terms.json', {'scope': LABELS})
    frames, plans = [], {}
    for index, symbol in enumerate(symbols):
        cash = 20 if index == 0 else 10 if second_dividend else 0
        prices = np.where(calendar >= ex, 100.0-cash, 100.0)
        frames.append(pd.DataFrame(dict(date=calendar, symbol=symbol,
            raw_open=prices, raw_close=prices, raw_high=prices+1, raw_low=prices-1,
            volume=100000, trade_status=1, is_st=0,
            limit_up=prices*1.1, limit_down=prices*.9)))
        events = [event(source, security_id=symbol, event_id='opening-'+str(index),
            revision=2, available_at=KNOWN, record_date=str(days[1].date()),
            day=str(ex.date()), pay_date=str(pay.date()), cash=Decimal(cash))] if cash else []
        plans[symbol] = plan_for(tmp_path / symbol, days, events=events, symbol=symbol)
    weights = (0, 1) if sell_on_ex else (.5, .5)
    snapshots = [(calendar[0], (.5, .5)), (days[1], weights)]
    if not pay_on_ex:
        snapshots.append((days[3], weights))
    snapshots.append((pay, weights))
    targets = pd.DataFrame([dict(date=day, symbol=symbol, target_weight=weight)
        for day, allocation in snapshots for symbol, weight in zip(symbols, allocation)])
    config = ExecutionBacktestConfig(initial_cash=100000)
    result = AShareExecutionBacktester(config, zero_fees()).run_target_weights(
        targets, pd.concat(frames, ignore_index=True),
        pd.DataFrame(dict(date=calendar, adj_open=100., adj_close=100.)), dividend_plans=plans)
    nav = result.nav.set_index('date')
    comparison = result.target_comparison.query('execution_date == @ex').set_index('symbol')
    outstanding = 15000 if second_dividend else 10000
    assert nav.loc[ex, 'turnover_denominator'] == 100000
    assert nav.loc[ex, 'opening_available_cash'] == 0
    assert comparison.target_shares.to_dict() == (
        {symbols[0]: 0, symbols[1]: 1100 if second_dividend else 1000} if sell_on_ex else
        {symbols[0]: 600, symbols[1]: 500})
    if not sell_on_ex:
        assert result.trades.query('execution_date == @ex').empty
        assert result.rejections.query('execution_date == @ex').reason.eq('cash_constraint').all()
        assert not result.rejections.query('execution_date == @ex').empty
        assert nav.loc[ex, 'market_value'] == 100000-outstanding
        assert comparison.loc[symbols[0], 'actual_weight'] == .4
        assert comparison.loc[symbols[1], 'actual_weight'] == (.45 if second_dividend else .5)
    assert nav.portfolio_value.eq(100000).all()
    if not pay_on_ex:
        assert nav.loc[ex, 'recognized_receivables'] == outstanding
        if not sell_on_ex:
            assert nav.loc[ex:pay, 'opening_available_cash'].eq(0).all()
            assert nav.loc[ex:days[3], 'available_cash'].eq(0).all()
    assert nav.loc[pay, 'recognized_receivables'] == 0
    assert nav.loc[pay, 'dividend_cash_flow'] == outstanding
    assert nav.loc[pay, 'available_cash'] - nav.loc[pay, 'opening_available_cash'] - nav.loc[pay, 'trade_cash_flow'] == outstanding
    assert nav.loc[days[days.get_loc(pay)+1], 'opening_available_cash'] == nav.loc[pay, 'available_cash']
    assert result.entitlements.eligible_shares.eq(500).all()
    assert len(result.recognitions) == len(result.payments) == len(result.entitlements) == (2 if second_dividend else 1)
    for name in ('cash_reconciliation', 'dividend_reconciliation', 'equity_reconciliation'):
        np.testing.assert_allclose(result.return_accounting[name], 0, atol=1e-8, rtol=0)


def test_multisecurity_shared_account(tmp_path):
    inputs = scenario(tmp_path)
    result = execute(inputs)
    days = inputs[0]
    assert len(result.nav) == 12
    assert result.nav.holdings.max() >= 2
    entitled = result.entitlements.set_index('event_id')
    assert entitled.loc['fixture-a', 'eligible_shares'] == 500
    assert entitled.loc['fixture-b', 'eligible_shares'] > 0
    assert entitled.loc['fixture-no-position', 'eligible_shares'] == 0
    assert not result.positions.query('date == @days[7] and symbol == @SYMBOLS[0]').shape[0]
    assert result.payments.set_index('event_id').loc['fixture-a', 'gross'] == Decimal(5000)
    assert result.payments.groupby('observed_on').gross.apply(lambda amounts: sum(amounts > 0)).max() >= 2
    assert result.rejections.reason.eq('cash_constraint').any()
    assert result.trades.slippage.gt(0).all()
    reconcile(result, inputs[4], inputs[5])
    reordered = (*inputs[:6], dict(reversed(list(inputs[6].items()))))
    repeated = execute(reordered)
    for name in ('nav', 'trades', 'positions', 'rejections', 'target_comparison', 'entitlements',
                 'payments', 'recognitions', 'return_accounting'):
        pd.testing.assert_frame_equal(getattr(result, name), getattr(repeated, name), check_exact=True)


def test_empty_plan_preserves_no_dividend_path(tmp_path):
    inputs = scenario(tmp_path, empty=True)
    plain, empty = execute(inputs, plans=False), execute(inputs)
    pd.testing.assert_frame_equal(plain.nav, empty.nav[plain.nav.columns], check_exact=True)
    for name in ('trades', 'positions', 'signals', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(plain, name), getattr(empty, name), check_exact=True)


def test_record_close_sale_has_no_entitlement(tmp_path):
    inputs = list(scenario(tmp_path))
    inputs[2] = inputs[2].copy()
    earlier = pd.DataFrame([dict(date=inputs[0][0], symbol=s, target_weight=w)
                           for s, w in zip(SYMBOLS, (0, .99, 0))])
    inputs[2] = pd.concat([inputs[2], earlier], ignore_index=True)
    result = execute(inputs)
    assert result.entitlements.set_index('event_id').loc['fixture-a', 'eligible_shares'] == 0


def test_payment_is_spendable_only_next_open(tmp_path):
    inputs = list(scenario(tmp_path, same_day=True))
    days = inputs[0]
    # Identical full-weight demands before, on, and after actual pay date.
    extra = pd.DataFrame([dict(date=day, symbol=s, target_weight=w)
        for day in (days[6],) for s, w in zip(SYMBOLS, (0, 1., 0))])
    inputs[2] = pd.concat([inputs[2], extra], ignore_index=True)
    result = execute(inputs)
    nav = result.nav.set_index('date')
    assert nav.loc[days[7], 'dividend_cash_flow'] > 0
    assert nav.loc[days[7], 'opening_available_cash'] == nav.loc[days[6], 'available_cash']
    assert nav.loc[days[8], 'opening_available_cash'] == nav.loc[days[7], 'available_cash']
    assert result.trades.query('execution_date == @days[8] and side == "buy"').executed_shares.sum() > 0
    assert result.rejections.query('execution_date == @days[7]').reason.eq('cash_constraint').any()
    reconcile(result, inputs[4], inputs[5])


@pytest.mark.parametrize('defect', ['missing_pay', 'duplicate', 'share_change', 'unknown', 'conflicting_cash'])
def test_invalid_event_refused_before_execution(tmp_path, defect):
    inputs = list(scenario(tmp_path))
    symbol = SYMBOLS[0]
    plan = inputs[6][symbol]
    action = plan.snapshot.events[0]
    if defect == 'duplicate':
        events = (action, action)
    else:
        fields = {'missing_pay': {'pay_date': None}, 'share_change': {'event_type': 'BONUS_SHARE'},
                  'unknown': {'event_type': 'UNRECOGNIZED'}, 'conflicting_cash': {'cash': Decimal(999)}}[defect]
        events = (replace(action, **fields),)
    inputs[6][symbol] = replace(plan, snapshot=replace(plan.snapshot, events=events))
    with pytest.raises((DataQualityError, SchemaError)):
        execute(inputs)


def test_incorrect_payment_receipt_fails_audit(tmp_path):
    inputs = scenario(tmp_path)
    result = execute(inputs)
    payments = result.payments.copy()
    payments.loc[0, 'gross'] += Decimal(1)
    with pytest.raises(AssertionError):
        reconcile(replace(result, payments=payments), inputs[4], inputs[5])


def test_missing_security_plan_refused(tmp_path):
    inputs = scenario(tmp_path)
    del inputs[6][SYMBOLS[2]]
    with pytest.raises(SchemaError, match='coverage'):
        execute(inputs)


def test_duplicate_identity_across_securities_refused(tmp_path):
    inputs = scenario(tmp_path)
    symbol = SYMBOLS[2]
    events = [replace(inputs[6][symbol].snapshot.events[0], event_id='fixture-a')]
    inputs[6][symbol] = plan_for(tmp_path / 'cross-security-duplicate', inputs[0],
                                events=events, symbol=symbol)
    with pytest.raises(SchemaError, match='across securities'):
        execute(inputs)


def test_adjusted_price_dividends_refused(tmp_path):
    inputs = list(scenario(tmp_path))
    inputs[1] = inputs[1].assign(adj_open=inputs[1].raw_open, adj_close=inputs[1].raw_close)
    inputs[4] = replace(inputs[4], open_price_column='adj_open', close_price_column='adj_close')
    with pytest.raises(SchemaError, match='adjusted prices double-count'):
        execute(inputs)


def test_same_day_positive_payments(tmp_path):
    inputs = scenario(tmp_path, same_day=True)
    result = execute(inputs)
    assert result.payments.groupby('observed_on').gross.apply(lambda amounts: sum(amounts > 0)).max() >= 3
    reconcile(result, inputs[4], inputs[5])
