from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.strategy_execution import AlpacaDecisionExecution
from factorlab.strategies.protocol import DecisionV2, decision_payload
from factorlab.strategies.v69_runtime import V69AlpacaStrategy
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from test_alpaca_instance_runtime import Account


def test_direct_intent_identity_and_cross_instance_cancellation(tmp_path):
    now = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    spec = replace(V69AlpacaStrategy('paper').describe(), strategy_id='example',
                   symbols=['SPY', 'IWM'], decision_kind='orders')
    binding = dict(market=spec.market, account_id='paper', strategy_id='example',
                   instance_id='alpha', allocation_id='alpha-usd')
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    executor = PortfolioExecutor(Account(), store, clock=lambda: now)
    executor.allocate(account_id='paper', strategy_id='alpha', allocation_id='alpha-usd',
                      capital=D(100), initial_cash=D(100), official_equity=D(100))
    adapter = AlpacaDecisionExecution(executor, SimpleNamespace(), spec, binding, {}, clock=lambda: now)
    intent = OrderIntent(Market.ALPACA_US_EQUITY, 'SPY', Side.BUY, OrderType.LIMIT,
                         D(2), D(10), 'original-strategy-client-id', time_in_force='DAY')
    receipt = dict(binding=binding, package_id='package', event_id='event', input_identity='inputs',
        event=dict(binding=binding, allocation={'positions': {}}, context={'execution_session': '2026-11-24'}),
        decision=decision_payload(DecisionV2('orders', orders=(intent,))))
    adapter.execute(receipt)
    adapter.execute(receipt)
    plan_id = store.db.execute('SELECT id FROM portfolio_plans').fetchone()[0]
    assert store.orders(plan_id)[0]['record'].snapshot.intent == intent
    assert store.cash('paper', 'alpha') == 80
    foreign = AlpacaDecisionExecution(executor, SimpleNamespace(), spec,
                                     dict(binding, instance_id='beta'), {}, clock=lambda: now)
    with pytest.raises(ValueError, match='another instance'):
        foreign._cancel({'symbol': 'SPY', 'client_order_id': intent.client_order_id})
    with pytest.raises(ValueError, match='another instance or asset'):
        adapter._cancel({'symbol': 'IWM', 'client_order_id': intent.client_order_id})
    assert len(store.orders(plan_id)) == 1
    executor.cancel = lambda plan, client_id: None
    with pytest.raises(TimeoutError, match='awaits original-order reconciliation'):
        adapter._cancel({'symbol': 'SPY', 'client_order_id': intent.client_order_id})
    store.close()


def test_market_order_uses_durable_execution_quote_without_signal_quote(tmp_path):
    from factorlab.trading.portfolio_execution import ExecutionQuote
    now = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    spec = replace(V69AlpacaStrategy('paper').describe(), strategy_id='example',
        symbols=['SPY'], decision_kind='orders',
        execution=dict(V69AlpacaStrategy('paper').describe().execution, order_types=['MARKET']))
    binding = dict(market=spec.market, account_id='paper', strategy_id='example',
        instance_id='alpha', allocation_id='alpha-usd')
    path = tmp_path / 'portfolio.sqlite3'
    store = PortfolioStore(path)
    executor = PortfolioExecutor(Account(), store, clock=lambda: now)
    executor.allocate(account_id='paper', strategy_id='alpha', allocation_id='alpha-usd',
        capital=D(100), initial_cash=D(100), official_equity=D(100))
    intent = OrderIntent(Market.ALPACA_US_EQUITY, 'SPY', Side.BUY, OrderType.MARKET,
        D(2), None, 'original-market-client-id', time_in_force='DAY')
    receipt = dict(binding=binding, package_id='package', event_id='event', input_identity='inputs',
        event=dict(binding=binding, allocation={'positions': {}}, symbols={},
            context={'execution_session': '2026-11-24'}),
        decision=decision_payload(DecisionV2('orders', orders=(intent,))))
    quote = ExecutionQuote('SPY', D(9), D(10), now, now, '2026-11-24', 'alpaca:iex')
    adapter = AlpacaDecisionExecution(executor, SimpleNamespace(), spec, binding, {},
        clock=lambda: now, quote_provider=lambda symbols: {'SPY': quote})
    executor.create = lambda **kwargs: (_ for _ in ()).throw(OSError('interrupted before outbox'))
    with pytest.raises(OSError, match='interrupted'):
        adapter.execute(receipt)
    assert store.db.execute('SELECT count(*) FROM portfolio_plans').fetchone()[0] == 0
    store.close()
    store = PortfolioStore(path)
    executor = PortfolioExecutor(Account(), store, clock=lambda: now)
    adapter = AlpacaDecisionExecution(executor, SimpleNamespace(), spec, binding, {},
        clock=lambda: now, quote_provider=lambda symbols: pytest.fail('recovery reacquired quote'))
    adapter.execute(receipt)
    adapter.execute(receipt)
    plan_id = store.db.execute('SELECT id FROM portfolio_plans').fetchone()[0]
    child, = store.orders(plan_id)
    assert child['record'].snapshot.intent == intent
    assert child['reservation_price'] == D(10)
    assert store.cash('paper', 'alpha') == D(80)
    assert receipt['event']['symbols'] == {}
    store.close()


@pytest.mark.parametrize('kind', ['target_quantities', 'quantity_deltas', 'target_weights'])
def test_frozen_receipt_replay_retains_intents_and_omitted_symbols(tmp_path, kind):
    now = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    spec = replace(V69AlpacaStrategy('paper').describe(), strategy_id='example',
                   symbols=['SPY', 'IWM'], decision_kind=kind)
    binding = dict(market=spec.market, account_id='paper', strategy_id='example',
                   instance_id='alpha', allocation_id='allocation-alpha')
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    for symbol in spec.symbols:
        store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                         ('paper', symbol, 'alpha', '3', '3', '100'))
    adapter = AlpacaDecisionExecution(PortfolioExecutor(None, store), SimpleNamespace(),
                                    spec, binding, {}, clock=lambda: now)
    targets = {'SPY': D('.2') if kind == 'target_weights' else D(5)}
    receipt = dict(binding=binding, package_id='package', event_id='event', input_identity='inputs',
        event=dict(binding=binding, allocation={'positions': {'SPY': D(3), 'IWM': D(3)}},
                   context={'execution_session': '2026-11-24'}),
        decision=decision_payload(DecisionV2(kind, targets=targets)))
    adapter.execute(receipt)
    store.db.execute("UPDATE portfolio_claims SET quantity='4' WHERE symbol='SPY'")
    adapter.execute(receipt)
    ids = store.db.execute('SELECT id FROM portfolio_plans').fetchall()
    assert len(ids) == 1
    request = store.plan(ids[0][0])['request']
    assert request['strategy'] == 'alpha'
    assert request['owned'] == ['SPY']
    if kind == 'target_quantities':
        assert request['quantity_deltas'] == {'SPY': D(2)}
    elif kind == 'quantity_deltas':
        assert request['quantity_deltas'] == {'SPY': D(5)}
    else:
        assert request['weights'] == targets
    foreign = dict(receipt, binding=dict(binding, instance_id='beta'))
    with pytest.raises(ValueError, match='another instance'):
        adapter.execute(foreign)
    store.close()
