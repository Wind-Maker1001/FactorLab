"""Cash-account sale fees must never create unsettled buying power."""
from copy import deepcopy

import pandas as pd
import pytest

from factorlab.cross_market.adapters import (
    ChinaAshareAdapter, DatedFees, FeePeriod, USEquityAdapter,
)
from factorlab.cross_market.calendar import cn_calendar, us_calendar
from factorlab.cross_market.engine import ExecutionEngine


DAY = pd.Timestamp('2024-06-03')
NEXT = pd.Timestamp('2024-06-04')


def book(cash=10., market='US'):
    fees = DatedFees([FeePeriod('2024-05-01', '2024-06-30', minimum=5.)], 'SYNTHETIC_MINIMUM')
    if market == 'US':
        adapter = USEquityAdapter(us_calendar('2024-05-01', '2024-06-14'), None, None, fees)
    else:
        adapter = ChinaAshareAdapter(cn_calendar(pd.bdate_range('2024-05-01', '2024-06-14'),
                                                'SYNTHETIC_ONLY'), None, None, fees)
    return ExecutionEngine(adapter, initial_cash=cash)


def order(e, side, quantity, price, day=DAY, iid='a'):
    return e.order(iid, side, quantity, {'raw_open': price, 'halted': False,
                                       'no_price_limit': True}, day, {'board': 'main'})


def held(e, iid='a', quantity=1., basis=2.):
    e.lots[iid] = [dict(quantity=quantity, basis=basis, date=pd.Timestamp('2024-05-30'))]


def invariant(e):
    locked = sum(p['amount'] for p in e.pending)
    assert all(p['amount'] > 0 for p in e.pending)
    assert 0 <= e.available_cash() <= e.cash + 1e-8
    if not e.adapter.rules.reuse_sale_proceeds:
        assert locked <= e.cash + 1e-8
        assert e.available_cash() == pytest.approx(e.cash - locked)


@pytest.mark.parametrize('price,net,available,bought', [
    (10., 5., 10., 5.), (5., 0., 10., 5.), (.1, -4.9, 5.1, 0.),
])
def test_sale_net_sign_preserves_ledger_and_subsequent_funding(price, net, available, bought):
    e = book()
    held(e)
    assert order(e, 'sell', 1, price) == 1
    assert e.cash == pytest.approx(10 + net)
    assert e.available_cash() == pytest.approx(available)
    assert e.pending == ([dict(due=NEXT, amount=net)] if net > 0 else [])
    assert e.quantity('a') == e.cost_basis('a') == 0
    trade = e.trades[-1]
    assert trade['fee'] == 5 and trade['released_basis'] == 2
    assert trade['realized_pnl'] == pytest.approx(net - 2)
    assert trade['cash'] == pytest.approx(10 + net)
    assert trade['available_cash'] == pytest.approx(available)
    invariant(e)
    # An attempted $6 purchase plus $5 commission must be clipped or rejected.
    assert order(e, 'buy', 6, 1, iid='b') == bought
    assert e.quantity('b') == bought
    assert e.cash == pytest.approx(10 + net - (bought + 5 if bought else 0))
    invariant(e)
    before = e.cash
    e.settle(NEXT)
    assert e.cash == before and not e.pending
    invariant(e)


def test_negative_sale_with_existing_unsettled_income_debits_only_settled_cash():
    e = book()
    held(e, 'a')
    held(e, 'b')
    assert order(e, 'sell', 1, 25) == 1  # $20 stays locked.
    assert order(e, 'sell', 1, .1, iid='b') == 1
    assert e.cash == pytest.approx(25.1) and e.available_cash() == pytest.approx(5.1)
    assert e.pending == [dict(due=NEXT, amount=20.)]
    assert order(e, 'buy', 1, 1, iid='c') == 0
    invariant(e)
    e.settle(NEXT)
    assert e.cash == pytest.approx(25.1) and e.available_cash() == pytest.approx(25.1)
    assert order(e, 'buy', 20, 1, NEXT, 'c') == 20
    assert e.cash == pytest.approx(.1)
    invariant(e)


@pytest.mark.parametrize('cash,locked', [(1., False), (1., True)])
def test_unfunded_sale_fee_rejects_without_mutating_holdings_or_spending_locked_cash(cash, locked):
    e = book(cash)
    held(e)
    if locked:
        held(e, 'b')
        assert order(e, 'sell', 1, 25, iid='b') == 1
    before = (e.cash, deepcopy(e.pending), deepcopy(e.lots), deepcopy(e.trades))
    assert order(e, 'sell', 1, .1) == 0
    assert (e.cash, e.pending, e.lots, e.trades) == before
    assert e.rejections[-1]['reason'] == 'INSUFFICIENT_CASH_FOR_FEES'
    assert order(e, 'buy', 1, 1, iid='c') == 0
    invariant(e)


def test_exactly_funded_negative_sale_uses_cash_once_and_rejects_next_buy():
    e = book(4.9)
    held(e)
    assert order(e, 'sell', 1, .1) == 1
    assert e.cash == pytest.approx(0.) and not e.pending
    assert order(e, 'buy', 1, 1, iid='b') == 0
    e.settle(NEXT)
    assert e.cash == pytest.approx(0.) and not e.pending
    invariant(e)


@pytest.mark.parametrize('day,due', [('2024-05-24', '2024-05-29'), ('2024-06-03', '2024-06-04')])
def test_positive_sale_remains_unspendable_until_actual_t2_or_t1_settlement(day, due):
    day, due = pd.Timestamp(day), pd.Timestamp(due)
    e = book(1.)
    held(e)
    assert order(e, 'sell', 1, 15, day) == 1
    assert e.cash == 11 and e.available_cash() == 1
    assert e.pending == [dict(due=due, amount=10.)]
    assert order(e, 'buy', 5, 1, day, 'b') == 0
    e.settle(due - pd.Timedelta(days=1))
    assert order(e, 'buy', 5, 1, day, 'b') == 0
    e.settle(due)
    assert e.cash == e.available_cash() == 11
    assert order(e, 'buy', 5, 1, due, 'b') == 5
    assert e.cash == 1 and e.cost_basis('b') == 10
    invariant(e)


def test_buy_sell_buy_sequence_cannot_recycle_sales_or_overdraw_cash():
    e = book(20.)
    assert order(e, 'buy', 5, 1) == 5
    assert e.cash == 10 and e.cost_basis('a') == 10
    assert order(e, 'sell', 5, .02) == 5
    assert e.cash == pytest.approx(5.1) and not e.pending
    assert e.trades[-1]['realized_pnl'] == pytest.approx(-14.9)
    assert order(e, 'buy', 1, 1, iid='b') == 0
    invariant(e)
    assert order(e, 'buy', 1, .1, iid='b') == 1
    assert e.cash == pytest.approx(0.)
    assert order(e, 'sell', 1, .1, iid='b') == 0
    assert e.quantity('b') == 1 and e.cost_basis('b') == pytest.approx(5.1)
    e.settle(NEXT)
    assert e.cash == pytest.approx(0.) and e.quantity('b') == 1
    invariant(e)


def test_cn_t1_share_sellability_and_same_day_sale_cash_reuse_are_preserved():
    e = book(2000., 'CN')
    assert order(e, 'buy', 100, 10) == 100
    assert order(e, 'sell', 100, 10) == 0
    assert e.rejections[-1]['reason'] == 'SHARES_NOT_SELLABLE'
    assert order(e, 'sell', 100, 10, NEXT) == 100
    assert e.cash == e.available_cash() == 1990.
    assert order(e, 'buy', 100, 10, NEXT, 'b') == 100
    assert e.cash == 985. and e.quantity('b') == 100
    invariant(e)


def test_cn_negative_sale_fee_is_immediate_and_never_debited_again():
    e = book(10., 'CN')
    held(e)
    assert order(e, 'sell', 1, .1) == 1
    assert e.cash == pytest.approx(5.1) and e.available_cash() == pytest.approx(5.1)
    assert order(e, 'buy', 100, .01, iid='b') == 0
    e.settle(NEXT)
    assert e.cash == pytest.approx(5.1) and not e.pending
    invariant(e)
