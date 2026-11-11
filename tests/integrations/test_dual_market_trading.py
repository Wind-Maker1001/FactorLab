"""Account-boundary and recovery tests using only synthetic offline transports."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from factorlab.trading import AssetBalance, OrderStatus
from factorlab.trading.control import RiskRejected
from factorlab.trading.dual_market import SyntheticAccounts, run
from factorlab.integrations.binance_equity_trading.paper import StockAdapter
from factorlab.integrations.binance_equity_trading.protocol import Unavailable
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents


@pytest.fixture
def accounts(tmp_path):
    value = SyntheticAccounts(tmp_path)
    yield value
    value.close()


def test_interleaved_execution_and_independent_restarts(tmp_path):
    result = run(tmp_path)
    assert result['futures']['reconciled'] and result['stocks']['reconciled']
    assert result['stock_request']['tokenize'] == 'false'
    assert result['settlement'] == 'UNKNOWN'
    assert result['fees'] == {'USDT': '0.2', 'USD': '0.0375'}
    assert result['futures']['account_id'] != result['stocks']['account_id']
    assert result['futures']['positions'][0]['instrument_id'] == 'NVDAUSDT'
    assert result['stocks']['positions'][0]['instrument_id'] == 'NVDA'


def test_cross_market_intents_events_and_kill_switch(accounts):
    f, s = accounts.futures, accounts.stocks
    fi, si = accounts.signals()
    assert not f.assess(si).approved and not s.assess(fi).approved
    s.broker.bind(si, accounts.observations, notional=D('75'))
    f.submit(fi)
    s.submit(si)
    fe = UserDataEvents(accounts.futures_adapter).parse(accounts.transport.trade_update(fi.client_order_id, '0.5'))
    se = s.fill(si.client_order_id, D('0.1'))
    before_f, before_s = f.account(), s.account()
    with pytest.raises(KeyError):
        s.replay(fe)
    with pytest.raises(KeyError):
        f.replay(se)
    assert f.account() == before_f and s.account() == before_s
    f.set_kill_switch()
    assert not s.broker.state.kill_switch
    assert s.assess(replace(si, client_order_id='stocks-second')).approved
    assert f.assess(replace(fi, client_order_id='futures-second')).reason == 'KILL_SWITCH'
    assert {a.currency for a in f.account().assets} == {'USDT'}
    assert {a.currency for a in s.account().assets} == {'USD'}
    assert s.account().assets[0].total == D('984.985')


def test_confirmation_then_local_commit_interruption(accounts, monkeypatch):
    f, s = accounts.futures, accounts.stocks
    fi, _ = accounts.signals()
    stocks_before = s.account()
    def interrupted(event):
        raise OSError('injected local commit interruption')
    with monkeypatch.context() as patch:
        patch.setattr(f.broker, '_confirm', interrupted)
        with pytest.raises(OSError):
            f.submit(fi)
    assert not f.account().open_orders
    with pytest.raises(SafetyBlocked, match='identities differ'):
        f.submit(replace(fi, client_order_id='replacement'))
    accounts.transport.trade_update(fi.client_order_id, '0.5')
    assert f.broker.recover(f).reconciled
    assert f.account().positions[0].quantity == D('0.5')
    assert f.submit(fi).cumulative_filled_quantity == D('0.5')
    assert len([c for c in accounts.transport.calls if c[0] == 'POST']) == 1
    assert s.account() == stocks_before


def test_unknown_never_resubmitted_and_other_account_continues(accounts):
    f, s = accounts.futures, accounts.stocks
    fi, si = accounts.signals()
    accounts.transport.timeout_next = True
    assert f.submit(fi) is None
    assert f.account().open_orders[0].status == OrderStatus.UNKNOWN
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        f.submit(fi)
    with pytest.raises(RiskRejected, match='RECONCILE_REQUIRED'):
        f.submit(replace(fi, client_order_id='replacement'))
    s.broker.bind(si, accounts.observations, notional=D('75'))
    s.submit(si)
    s.fill(si.client_order_id, D('0.5'))
    stocks_before = s.account()
    assert f.broker.recover(f).reconciled
    assert len([c for c in accounts.transport.calls if c[0] == 'POST']) == 1
    assert s.account() == stocks_before


def test_restarted_stock_snapshot_conflict_blocks_only_stocks(accounts):
    f, s = accounts.futures, accounts.stocks
    _, si = accounts.signals()
    s.broker.bind(si, accounts.observations, notional=D('75'))
    s.submit(si)
    s.fill(si.client_order_id, D('0.2'))
    expected = s.account()
    accounts.stocks = restored = accounts.stocks_controller()
    wrong = replace(expected, assets=(AssetBalance('USD', D('999'), expected.assets[0].reserved),))
    with pytest.raises(Unavailable, match='CONFLICT'):
        StockAdapter(restored).reconcile(wrong)
    assert not restored.account().reconciled
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        restored.submit(si)
    assert not f.broker.state.kill_switch and f.account().reconciled
    assert StockAdapter(restored).reconcile(expected).reconciled
    assert restored.broker.state.kill_switch
    assert restored.account().assets == expected.assets


@pytest.mark.parametrize('market', ['futures', 'stocks'])
@pytest.mark.parametrize('gate', ['stale', 'budget', 'kill'])
def test_shared_risk_gates_leave_both_ledgers_unchanged(accounts, market, gate):
    c = getattr(accounts, market)
    fi, si = accounts.signals()
    intent = fi if market == 'futures' else si
    if gate == 'stale':
        c.clock = c.broker.clock = lambda: NOW + timedelta(seconds=6)
        reason = 'STALE_QUOTE'
    elif gate == 'budget':
        c.limits = replace(c.limits, account_budget=D('1'))
        reason = 'ACCOUNT_BUDGET_LIMIT'
    else:
        c.set_kill_switch()
        reason = 'KILL_SWITCH'
    before = (accounts.futures.account(), accounts.stocks.account())
    with pytest.raises(RiskRejected, match=reason):
        c.submit(intent)
    assert (accounts.futures.account(), accounts.stocks.account()) == before
    assert not [call for call in accounts.transport.calls if call[0] == 'POST']


def test_stock_untrusted_time_does_not_touch_futures(accounts):
    _, si = accounts.signals()
    before = accounts.futures.account()
    with pytest.raises(Unavailable, match='SOURCE_TIME'):
        accounts.stocks.broker.bind(si, replace(accounts.observations, quote_time=None), notional=D('75'))
    assert not accounts.stocks.account().open_orders
    assert accounts.futures.account() == before
