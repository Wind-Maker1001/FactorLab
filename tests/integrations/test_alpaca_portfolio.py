from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.portfolio import AlpacaPortfolioRunner
from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market, OrderStatus as S, Position
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, PortfolioStore

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


class Missing(LookupError):
    pass


class Adapter:
    def __init__(self):
        self.orders = {}
        self.store = SimpleNamespace(order=self.orders.get, get=lambda key: None)
        self.details = {}
        self.activities = {}
        self.cash = D(100)
        self.positions = {}
        self.posts = []
        self.publish_activities = True
        self.die_after_post = False

    def recover(self):
        return self.read_account()

    def read_account(self):
        return AccountSnapshot(Market.BINANCE_EQUITY, 'paper', (AssetBalance('USD', self.cash),),
            tuple(Position(s, q, D(10), D(10), 'USD') for s, q in self.positions.items()), (), NOW, True)

    def read_account_details(self):
        return SimpleNamespace(account_id='paper', equity=D(100), cash=self.cash, buying_power=D(200))

    def submit_order(self, intent, *, reservation_price):
        assert intent.client_order_id not in self.orders
        self.orders[intent.client_order_id] = intent
        self.posts.append(intent)
        quantity, price = intent.quantity, intent.limit_price
        self.cash -= quantity * price
        self.positions[intent.instrument_id] = quantity
        self.details[intent.client_order_id] = dict(client_order_id=intent.client_order_id,
            exchange_order_id='official-' + intent.client_order_id, status=S.FILLED,
            filled_quantity=quantity, filled_avg_price=price.quantize(D('.01')),
            cumulative_filled_notional=quantity * price.quantize(D('.01')), observed_at=NOW)
        self.activities[intent.client_order_id] = [dict(id='activity-' + intent.client_order_id,
            order_id='official-' + intent.client_order_id, qty=str(quantity), price=str(price),
            symbol=intent.instrument_id, side='buy', transaction_time=NOW.isoformat())]
        if self.die_after_post:
            raise SystemExit('E01 consumed response; E03 has not committed it')
        # A different consumer has already consumed all fills. Its incremental
        # OrderEvent would now have fill_price=None; the bridge must not use it.
        return None

    def read_order_details(self, identity):
        if identity not in self.details:
            raise Missing(identity)
        return self.details[identity]

    def read_order_executions(self, identity):
        return self.activities[identity] if self.publish_activities else []


def inputs():
    return dict(quotes={'AAPL': ExecutionQuote('AAPL', D('10.1234'), D('10.1234'),
        NOW, NOW, 'test-session', 'official-test-fixture')},
        rules={'AAPL': InstrumentRules(D('.001'), D(1))})


def create(runner):
    return runner.executor.create(account_id='paper', strategy_id='ALP-E03 TEST', strategy_version='1',
        trading_session='test-session', weights={'AAPL': D('.5')}, owned_symbols={'AAPL'}, now=NOW)


def test_bridge_restores_cash_exact_fill_after_e01_consumed_response(tmp_path):
    path = tmp_path / 'portfolio.sqlite'
    adapter = Adapter()
    store = PortfolioStore(path)
    runner = AlpacaPortfolioRunner(adapter, store, order_not_found=Missing, clock=lambda: NOW)
    identity = create(runner)
    adapter.die_after_post = True
    with pytest.raises(SystemExit):
        runner.run_once(identity, **inputs())
    store.close()
    adapter.die_after_post = False
    recovered = PortfolioStore(path)
    runner = AlpacaPortfolioRunner(adapter, recovered, order_not_found=Missing, clock=lambda: NOW)
    assert runner.run_once(identity, **inputs()).phase == 'COMPLETE'
    assert len(adapter.posts) == 1
    order = recovered.orders(identity)[0]
    assert order['filled_notional'] == adapter.posts[0].quantity * D('10.1234')
    report = runner.report(identity)
    assert report.traded_notional == order['filled_notional']
    assert report.initial_capital == 100
    assert report.official_cash == adapter.cash
    assert report.tracking_error > 0
    assert recovered.plan(identity)['report']['traded_notional'] == report.traded_notional
    recovered.close()


def test_lagging_activities_block_without_fabricated_price_or_duplicate(tmp_path):
    adapter = Adapter()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite')
    runner = AlpacaPortfolioRunner(adapter, store, order_not_found=Missing, clock=lambda: NOW)
    identity = create(runner)
    adapter.publish_activities = False
    assert runner.run_once(identity, **inputs()).phase == 'WAITING'
    assert runner.run_once(identity, **inputs()).phase == 'WAITING'
    assert len(adapter.posts) == 1
    assert store.orders(identity)[0].get('filled_notional', D(0)) == 0
    adapter.publish_activities = True
    assert runner.run_once(identity, **inputs()).phase == 'COMPLETE'
    assert len(adapter.posts) == 1
    store.close()


def test_committed_marker_before_entering_e01_can_resume_first_post(tmp_path):
    adapter = Adapter()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite')
    runner = AlpacaPortfolioRunner(adapter, store, order_not_found=Missing, clock=lambda: NOW)
    identity = create(runner)
    # Kill at the executor's second transaction, after intent preparation but
    # before it can enter the adapter's admission journal.
    original = runner.bridge.query_order
    count = 0

    def die_before_http(client_id):
        nonlocal count
        count += 1
        if count == 2:
            raise SystemExit('between committed marker and E01 entry')
        return original(client_id)

    runner.bridge.query_order = die_before_http
    with pytest.raises(SystemExit):
        runner.run_once(identity, **inputs())
    assert store.orders(identity)[0]['attempted']
    assert not adapter.posts
    runner.bridge.query_order = original
    runner.run_once(identity, **inputs())
    assert len(adapter.posts) == 1
    store.close()


def test_activity_symbol_identity_is_enforced(tmp_path):
    adapter = Adapter()
    store = PortfolioStore(tmp_path / 'portfolio.sqlite')
    runner = AlpacaPortfolioRunner(adapter, store, order_not_found=Missing, clock=lambda: NOW)
    identity = create(runner)
    runner.run_once(identity, **inputs())
    client_id = adapter.posts[0].client_order_id
    adapter.activities[client_id][0]['symbol'] = 'QQQ'
    with pytest.raises(ValueError, match='another instrument'):
        runner.bridge.query_order(client_id)
    store.close()


def test_market_data_binding_keeps_invalid_symbol_isolated():
    from datetime import timedelta
    from factorlab.integrations.alpaca.portfolio import portfolio_quote
    quote = SimpleNamespace(instrument=SimpleNamespace(instrument_id='us:aapl', symbol='AAPL'),
        bid=D(0), ask=D(0), event_at=NOW, received_at=NOW, session=NOW,
        source='alpaca', feed='iex', valid=False, validity_reasons=('invalid_spread',),
        decision_window=(NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)))
    result = portfolio_quote(quote, instrument_id='us:aapl', symbol='AAPL')
    assert result.instrument_id == 'AAPL' and not result.valid
    assert result.reason == 'invalid_spread' and result.source == 'alpaca:iex'
    with pytest.raises(ValueError, match='mapping mismatch'):
        portfolio_quote(quote, instrument_id='us:qqq', symbol='QQQ')
