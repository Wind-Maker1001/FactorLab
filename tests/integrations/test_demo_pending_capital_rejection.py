"""An underfunded Demo receipt yields to another owner in the same execution turn."""
from contextlib import closing
from decimal import Decimal as D
import json
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.trading import Market, OrderStatus
from factorlab.trading.control import RiskLimits
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, PortfolioStore
from factorlab.trading.storage import decode
from test_binance_demo_execution import NOW, intent
from test_binance_demo_network import wired
from test_binance_demo_strategy_session import deployment


@pytest.mark.parametrize(('quantity', 'limit', 'reason'), [
    ('.08', None, 'direct orders exceed allocated cash'),
    ('.04', D(3), 'ORDER_NOTIONAL_LIMIT'),
])
def test_underfunded_accepted_decision_does_not_block_another_owner(deployment, monkeypatch, quantity, limit, reason):
    root, credentials, campaign, exchange = deployment
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        runtime.start_continuous(credentials.read_config(), opening_equity=D(1000),
                                 stop_epoch=runtime.stop_epoch())
        with (open_session(root, credentials, runtime, counts, campaign=campaign) as (session, _),
              closing(PortfolioStore(root / 'pending-capital.sqlite3')) as capital):
            monkeypatch.setattr('factorlab.integrations.binance_futures_trading.network.datetime',
                                SimpleNamespace(now=lambda tz: session._now()))
            monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_runtime.now',
                                session._now)
            capability = DemoInstanceCapability(session, runtime, dispatch_gate=lambda binding: None)
            owner = DemoInstanceOwner.open(session, capital, capability=capability)
            execution = owner.execution
            session.gateway.recover(session.controller)
            account_id = session.adapter.account_id
            symbol = 'NVDAUSDT'
            capital.capital_policy.configure(account_id=account_id, currency='USDT',
                budget=D(27), fee_rate=D('.001'), mode='SHARED_SAME_DIRECTION')

            receipts = []
            for instance, size in [('underfunded', quantity), ('healthy', '.04')]:
                binding = dict(market=Market.BINANCE_USDM.value, account_id=account_id,
                    instance_id=instance, allocation_id=instance + '-cash', strategy_id='test-strategy')
                execution.executor.allocate(account_id=account_id, strategy_id=instance,
                    allocation_id=binding['allocation_id'], capital=D(7), initial_cash=D(7),
                    official_equity=D(1000))
                spec = SimpleNamespace(account_id=account_id, market=binding['market'],
                    strategy_id=binding['strategy_id'], symbols=[symbol],
                    capital=dict(currency='USDT', requested=D(7)),
                    execution=dict(shorting=False, order_types=['LIMIT'], time_in_force=['GTC']))
                execution.qualify(spec, binding, dict())
                if instance == 'underfunded' and limit is not None:
                    capital.capital_policy.set_limits(account_id, instance, RiskLimits(limit, D(7), D(7)))
                receipt = dict(binding=binding, package_id='test-package', event_id=instance + '-signal',
                    input_identity=instance + '-input', event={},
                    decision=dict(kind='orders', orders=[intent(instance, limit='100', quantity=size)],
                                  targets={}, cancellations=[], state={}))
                execution.execute(receipt)
                receipts.append(receipt)

            pending = execution.store.db.execute(
                'SELECT id,payload FROM demo_instance_outbox WHERE completed=0 ORDER BY rowid').fetchall()
            (rejected_id, rejected_raw), (healthy_id, healthy_raw) = pending
            rejected_child = decode(json.loads(rejected_raw))['intents'][0].client_order_id
            healthy_child = decode(json.loads(healthy_raw))['intents'][0].client_order_id
            at = NOW
            quotes = {symbol: ExecutionQuote(symbol, D(100), D(100), at, at, at.date().isoformat(), 'OFFLINE_HTTP')}
            rules = {symbol: InstrumentRules(D('.01'), D(1), price_tick=D('.01'))}
            results = owner.advance(equity=D(1000), quotes=quotes, rules=rules, now=at)

            assert results[rejected_id] is None
            assert results[healthy_id].order_ids == (healthy_child,)
            assert exchange.orders[healthy_child]['status'] == 'NEW'
            assert counts['POST'] == 1
            assert set(session._protocol_ids()) == {healthy_child}
            assert capital.db.execute('SELECT id FROM portfolio_orders').fetchall() == [(healthy_child,)]
            assert execution.owners.owner(rejected_child) == ('underfunded', 'underfunded-cash', symbol)

            execution.execute(receipts[0])
            completed, raw = execution.store.db.execute(
                'SELECT completed,payload FROM demo_instance_outbox WHERE id=?', (rejected_id,)).fetchone()
            rejected = decode(json.loads(raw))
            assert completed == 2
            assert rejected['rejection_reason'] == reason
            assert rejected['receipt'] == receipts[0]
            local_event = decode(json.loads(execution.store.event_payload('demo:capital-rejection:' + rejected_child)))
            assert local_event.status is OrderStatus.REJECTED
            assert local_event.client_order_id == rejected_child
            assert local_event.exchange_order_id is None and local_event.cumulative_filled_quantity == 0
