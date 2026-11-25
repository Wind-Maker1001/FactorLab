from decimal import Decimal as D
import json

import pytest

from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from factorlab.trading.storage import decode
from test_portfolio_allocations import allocate, plan
from test_portfolio_execution import Broker, NOW, run


def legacy(tmp_path, *, partial=False):
    broker = Broker()
    source = PortfolioStore(tmp_path / 'original.sqlite3')
    executor = PortfolioExecutor(broker, source, clock=lambda: NOW)
    allocate(executor, 'one', '40')
    identity = plan(executor, 'one')
    if partial:
        broker.mode = 'partial'
    run(executor, identity)
    if not partial:
        run(executor, identity)
    orders = source.orders(identity)
    events = [decode(json.loads(raw)) for raw, in source.db.execute(
        'SELECT payload FROM portfolio_confirmations ORDER BY rowid')]
    sleeve = dict(instance_id='one', logical_strategy='original-package', allocation_id='one-cash',
        capital=D(40), initial_cash=D(40), initial_inventory={}, owned_symbols=['AAPL'],
        created_at=NOW, trading_session='2026-11-24', orders=orders, events=events, source_receipt=dict(
            original_plan=identity, history_complete=True,
            order_ids=[order['record'].snapshot.intent.client_order_id for order in orders],
            event_ids=[event.event_id for event in events]))
    source.close()
    return broker, sleeve


def destination(tmp_path, broker):
    store = PortfolioStore(tmp_path / 'migrated.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    return store, executor


def test_original_fills_and_receipt_migrate_once_without_resubmission(tmp_path):
    broker, sleeve = legacy(tmp_path)
    store, executor = destination(tmp_path, broker)
    args = dict(receipt_id='original-authorization', account_id='paper', sleeves=[sleeve], official_equity=D(100))
    executor.adopt_existing(**args)
    executor.adopt_existing(**args)
    assert store.holdings('paper', 'one') == {'AAPL': D(4)}
    assert store.cash('paper', 'one') == 0
    original_ids = [intent.client_order_id for intent in broker.posts]
    assert [identity for identity, in store.db.execute('SELECT id FROM portfolio_orders')] == original_ids
    identity = store.db.execute('SELECT id FROM portfolio_plans').fetchone()[0]
    assert run(executor, identity).phase == 'COMPLETE'
    assert [intent.client_order_id for intent in broker.posts] == original_ids
    assert executor.report(identity, official_equity=D(100), readonly=True).positions[0].owned_cost_basis == D(40)
    store.close()
    store = PortfolioStore(tmp_path / 'migrated.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    executor.adopt_existing(**args)
    with pytest.raises(ValueError, match='receipt identity changed'):
        executor.adopt_existing(**dict(args, sleeves=[dict(sleeve, initial_cash=D(41))]))
    store.close()


def test_partial_order_migration_preserves_remaining_reservation_and_owned_cancel(tmp_path):
    broker, sleeve = legacy(tmp_path, partial=True)
    store, executor = destination(tmp_path, broker)
    executor.adopt_existing(receipt_id='partial', account_id='paper', sleeves=[sleeve], official_equity=D(100))
    assert store.holdings('paper', 'one') == {'AAPL': D(2)}
    assert store.cash('paper', 'one') == 0
    assert executor.capital_policy.snapshot('paper', 'one').reserved == D(20)
    identity = store.db.execute('SELECT id FROM portfolio_plans').fetchone()[0]
    executor.cancel(identity, broker.posts[0].client_order_id)
    assert store.cash('paper', 'one') == D(20)
    assert len(broker.posts) == 1
    store.close()


def test_existing_owner_allocation_rebases_cash_without_reapplying_fills(tmp_path):
    broker = Broker()
    store = PortfolioStore(tmp_path / 'adopt.sqlite3')
    executor = PortfolioExecutor(broker, store, clock=lambda: NOW)
    identity = plan(executor, 'one')
    run(executor, identity)
    run(executor, identity)
    # The legacy owner has already invested the whole account; current cash is zero.
    with pytest.raises(ValueError, match='adopt_allocation'):
        executor.allocate(account_id='paper', strategy_id='one', allocation_id='one-cash',
            capital=D(100), initial_cash=D(0), official_equity=D(100))
    args = dict(account_id='paper', strategy_id='one', allocation_id='one-cash',
                capital=D(100), current_cash=D(0), official_equity=D(100), receipt_id='adopt-original')
    executor.adopt_allocation(**args)
    executor.adopt_allocation(**args)
    assert store.cash('paper', 'one', reserve=False) == 0
    assert store.allocation('paper', 'one')['initial_cash'] == D(100)
    assert executor.report(identity, official_equity=D(100), readonly=True).economic_nav == D(100)
    assert len(broker.posts) == 1
    store.close()


def test_incomplete_confirmation_history_rolls_back_all_allocations_and_claims(tmp_path):
    broker, sleeve = legacy(tmp_path)
    store, executor = destination(tmp_path, broker)
    missing = dict(sleeve, events=[])
    with pytest.raises(ValueError, match='identity evidence'):
        executor.adopt_existing(receipt_id='incomplete', account_id='paper', sleeves=[missing], official_equity=D(100))
    assert store.holdings('paper') == {}
    assert store.allocation('paper', 'one') is None
    assert store.db.execute('SELECT count(*) FROM portfolio_orders').fetchone()[0] == 0
    store.close()
