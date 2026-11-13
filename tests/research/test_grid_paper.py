"""Atomic Grid execution through the unified Paper authority."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules, GridConfirmationConflict
from factorlab.trading.contracts import Market, OrderStatus as S, Side, RiskDecision
from factorlab.trading.control import TradingController, RiskLimits
from factorlab.trading.paper import Instrument, PaperBroker
from factorlab.trading.storage import PaperStore, dumps


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def setup(path, *, config=None, rules=None):
    config = config or GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, fee_bps=10, slippage_bps=0,
        max_drawdown=1, max_turnover=100, breakout='exit')
    rules = rules or GridRules(D(1), D('.1'), D('.1'), D('.1'), D(1))
    clock = [NOW]
    broker = PaperBroker(Market.BINANCE_EQUITY, 'grid-paper', (Instrument('TEST', 'USD'),),
                         {'USD': D(1000)}, fee_rate=D('.001'))
    controller = TradingController(broker, PaperStore(path), clock=lambda: clock[0],
        limits=RiskLimits(D(1000), D(1000), D(1000), 100))
    grid = GridExecution(config, rules, deployment_id='grid', account_id='grid-paper',
        market=broker.state.market, instrument_id='TEST', initial_equity=D(1000))
    return grid, controller, clock


def observe(grid, controller, clock, n, price, *, completed=False):
    clock[0] = NOW + timedelta(days=n)
    grid.observe_paper(controller, dict(id=str(n), time=clock[0].replace(tzinfo=None).isoformat(),
        price=price, volume=10, completed_bar=completed, high=price, low=price, close=price),
        delivery=grid.delivery+1, observed_at=clock[0])


def submit(grid, controller, price=90):
    key = next(k for k, o in grid.orders.items() if o.intent.limit_price == price)
    grid.submit_paper(controller, key, delivery=grid.delivery+1)
    return key


def fill(grid, controller, key, quantity, price):
    return grid.fill_paper(controller, key, D(quantity), D(price), delivery=grid.delivery+1)


def test_expiry_reseed_confirmed_once_and_restart(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    path = tmp_path/'paper.db'
    grid, controller, clock = setup(path, config=config)
    observe(grid, controller, clock, 0, 100)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    old = set(grid.orders)
    observe(grid, controller, clock, 1, 100, completed=True)
    assert grid.expiry_reseeds == 0
    for key in sorted(grid.cancellations):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    assert grid.expiry_reseeds == 1 and not old.intersection(grid.orders)
    issued = grid.issued_orders
    controller.store.close()
    restored, controller, clock = setup(path, config=config)
    restored.restore(controller)
    assert restored.expiry_reseeds == 1 and restored.issued_orders == issued
    assert controller.reconcile().reconciled
    observe(restored, controller, clock, 2, 100, completed=True)
    assert restored.issued_orders == issued
    controller.store.close()


def test_partial_expiry_reseed_waits_for_unknown(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    key = next(iter(grid.orders))
    fill(grid, controller, key, 1, 80)
    observe(grid, controller, clock, 1, 100)
    controller.mark_timeout(key)
    with pytest.raises(ValueError, match='reconciliation|authority|RECONCILE|checkpoint'):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    assert grid.expiry_reseeds == 0
    assert controller.broker.state.positions['TEST'].quantity == 1
    controller.store.close()


def test_reseed_partial_expiry_preserves_inventory_and_reservations(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100, completed=True)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    key = next(k for k, o in grid.orders.items() if o.intent.limit_price == 90)
    fill(grid, controller, key, 1, 90)
    for identity, order in list(grid.orders.items()):
        if order.status == S.NEW:
            grid.submit_paper(controller, identity, delivery=grid.delivery+1)
    observe(grid, controller, clock, 1, 100, completed=True)
    old = set(grid.orders)
    for identity in sorted(grid.cancellations):
        grid.cancel_paper(controller, identity, delivery=grid.delivery+1)
    assert grid.expiry_reseeds == 1 and not old.intersection(grid.orders)
    assert grid.account.inventory == 1 and grid.account.committed == 90
    assert sum(o.intent.quantity for o in grid.active if o.intent.side == Side.SELL) <= 1
    assert controller.broker.state.positions['TEST'].quantity == 1
    controller.store.close()


def test_reseed_cooldown_completed_bar_bound_and_idempotency(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=1,
        expiry_reseed_interval=3, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    observe(grid, controller, clock, 1, 100)
    for key in sorted(grid.cancellations):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    assert grid.expiry_reseeds == 0 and grid.pending_expiry_reseed
    observe(grid, controller, clock, 2, 100, completed=True)
    assert grid.expiry_reseeds == 1
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    observe(grid, controller, clock, 3, 100, completed=True)
    for key in sorted(grid.cancellations):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    assert grid.state == 'PAUSED' and grid.reason == 'EXPIRY_RESEED_LIMIT'
    issued = grid.issued_orders
    observe(grid, controller, clock, 4, 100, completed=True)
    delivery = grid.delivery
    grid.observe_paper(controller, dict(id='4', time=clock[0].replace(tzinfo=None).isoformat(),
        price=100, volume=10, completed_bar=True, high=100, low=100, close=100),
        delivery=delivery, observed_at=clock[0])
    assert grid.expiry_reseeds == 1 and grid.issued_orders == issued
    controller.store.close()


def test_reseed_cancel_commit_failure_is_atomic(tmp_path, monkeypatch):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100, completed=True)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    observe(grid, controller, clock, 1, 100, completed=True)
    keys = sorted(grid.cancellations)
    for key in keys[:-1]:
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    before = controller.store.load()
    revision, delivery, issued = grid._revision, grid.delivery, grid.issued_orders
    save = controller.store.save

    def fail(state):
        save(state)
        raise RuntimeError('injected reseed transaction failure')

    with monkeypatch.context() as patch:
        patch.setattr(controller.store, 'save', fail)
        with pytest.raises(RuntimeError, match='injected reseed'):
            grid.cancel_paper(controller, keys[-1], delivery=delivery+1)
    assert controller.store.load() == before
    assert (grid._revision, grid.delivery, grid.issued_orders) == (revision, delivery, issued)
    assert grid.expiry_reseeds == 0
    grid.cancel_paper(controller, keys[-1], delivery=delivery+1)
    assert grid.expiry_reseeds == 1
    controller.store.close()


def test_reseed_without_admissible_orders_pauses(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100, completed=True)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    observe(grid, controller, clock, 1, 80, completed=True)
    for key in sorted(grid.cancellations):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    assert grid.state == 'PAUSED' and grid.reason == 'EXPIRY_RESEED_BLOCKED'
    assert not grid.orders and grid.expiry_reseeds == 1
    observe(grid, controller, clock, 2, 100, completed=True)
    assert grid.expiry_reseeds == 1 and not grid.orders
    controller.store.close()


def test_reseed_controller_rejection_commits_pause_not_repeated_proposals(tmp_path, monkeypatch):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=2, order_ttl=1, expiry_reseed_limit=2,
        expiry_reseed_interval=1, fee_bps=0, slippage_bps=0)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100, completed=True)
    for key in list(grid.orders):
        grid.submit_paper(controller, key, delivery=grid.delivery+1)
    observe(grid, controller, clock, 1, 100, completed=True)
    for key in sorted(grid.cancellations):
        grid.cancel_paper(controller, key, delivery=grid.delivery+1)
    monkeypatch.setattr(controller, 'assess', lambda _: RiskDecision(False, 'ACCOUNT_BUDGET_LIMIT'))
    with pytest.raises(ValueError, match='ACCOUNT_BUDGET_LIMIT'):
        grid.submit_paper(controller, next(iter(grid.orders)), delivery=grid.delivery+1)
    assert grid.state == 'PAUSED' and grid.reason == 'EXPIRY_RESEED_ACCOUNT_BUDGET_LIMIT'
    assert not grid.orders and grid.expiry_reseeds == 1
    issued = grid.issued_orders
    observe(grid, controller, clock, 2, 100, completed=True)
    assert grid.issued_orders == issued
    controller.store.close()


def test_incremental_fills_fees_and_duplicate_delivery(tmp_path):
    grid, controller, clock = setup(tmp_path/'paper.db')
    observe(grid, controller, clock, 0, 100)
    key = submit(grid, controller)
    first = fill(grid, controller, key, 1, 89)
    assert first.status == S.PARTIALLY_FILLED
    assert first.cumulative_filled_quantity == 1 and first.cumulative_fee == D('.089')
    before = controller.store.load()
    assert grid.fill_paper(controller, key, D(1), D(89), delivery=grid.delivery) is None
    assert controller.store.load() == before
    second = fill(grid, controller, key, 5, 90)
    assert second.status == S.FILLED and second.cumulative_filled_quantity == 2
    assert second.cumulative_fee == D('.179')
    assert grid.account.inventory == 2 and grid.account.turnover == 179
    assert controller.broker.state.cash['USD'] == D('820.821')
    assert grid.account.equity == D('1020.821')
    assert sum(o.intent.quantity for o in grid.active if o.intent.side == Side.SELL) == 2
    controller.store.close()


@pytest.mark.parametrize('allowance,expected', [('135', '1.5'), ('94', '1'), ('8', '0')])
def test_actual_price_turnover_clips_and_obeys_minimum(tmp_path, allowance, expected):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0, initial_cash=1000,
        quantity=2, fee_bps=0, slippage_bps=0, max_turnover=float(D(allowance)/1000), max_drawdown=1)
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100)
    key = submit(grid, controller)
    event = fill(grid, controller, key, 2, 90)
    quantity = D(expected)
    if quantity:
        assert event.cumulative_filled_quantity == quantity
    else:
        assert event is None and controller.broker.state.sequence == 1
    assert grid.account.turnover == quantity*90 <= D(allowance)
    assert grid.account.inventory == quantity
    assert fill(grid, controller, key, 2, 90) is None
    assert controller.broker.state.orders[key].snapshot.cumulative_filled_quantity == quantity
    controller.store.close()


def test_exit_after_turnover_exhaustion_and_cancellation_race(tmp_path):
    config = GridConfig(lower=80, upper=120, levels=4, initial_inventory=0, initial_cash=1000,
        quantity=2, fee_bps=0, slippage_bps=0, max_turnover=.18, max_drawdown=1, breakout='exit')
    grid, controller, clock = setup(tmp_path/'paper.db', config=config)
    observe(grid, controller, clock, 0, 100)
    key = submit(grid, controller)
    fill(grid, controller, key, 1, 90)
    # Breakout requests cancellation before the transport confirms it.
    observe(grid, controller, clock, 1, 70)
    assert grid.state == 'EXITING' and key in grid.cancellations
    late = fill(grid, controller, key, 1, 90)
    assert late.status == S.FILLED and grid.account.turnover == 180
    assert grid.state == 'EXITING'
    for order_id in tuple(grid.cancellations):
        grid.cancel_paper(controller, order_id, delivery=grid.delivery+1)
    exit_id = next(k for k, o in grid.orders.items() if o.intent.limit_price is None)
    grid.submit_paper(controller, exit_id, delivery=grid.delivery+1)
    partial = fill(grid, controller, exit_id, 1, 70)
    assert partial.status == S.PARTIALLY_FILLED and grid.state == 'EXITING'
    assert grid.account.inventory == 1
    closed = fill(grid, controller, exit_id, 20, 70)
    assert closed.cumulative_filled_quantity == 2
    assert closed.status == S.FILLED and grid.state == 'EXITED'
    assert grid.account.inventory == 0 and grid.account.turnover == 320
    assert controller.broker.state.cash['USD'] == D('959.680')
    controller.store.close()


def test_commit_failure_rolls_back_fill_fee_turnover_and_grid(tmp_path, monkeypatch):
    grid, controller, clock = setup(tmp_path/'paper.db')
    observe(grid, controller, clock, 0, 100)
    key = submit(grid, controller)
    store = controller.store
    before, revision, delivery = store.load(), grid._revision, grid.delivery
    save = store.save

    def deny_commit(action, argument, *_):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and argument == 'COMMIT' else sqlite3.SQLITE_OK

    def fail(state):
        save(state)
        store.db.set_authorizer(deny_commit)

    with monkeypatch.context() as patch:
        patch.setattr(store, 'save', fail)
        with pytest.raises(sqlite3.DatabaseError, match='authorized'):
            fill(grid, controller, key, 1, 90)
    store.db.set_authorizer(None)
    assert store.load() == controller.broker.state == before
    assert grid.delivery == delivery and grid._revision == revision
    assert grid.account.turnover == 0 and grid.account.inventory == 0
    assert len(list(store.order_events())) == 1
    done = fill(grid, controller, key, 1, 90)
    assert done.cumulative_filled_quantity == 1 and done.cumulative_fee == D('.09')
    assert grid.account.turnover == 90 and grid.account.inventory == 1
    store.close()
    restored, controller, clock = setup(tmp_path/'paper.db', config=grid.config)
    restored.restore(controller)
    assert restored.account.turnover == 90 and restored.account.inventory == 1
    assert restored.orders[key].status == S.UNKNOWN
    assert controller.reconcile().reconciled
    observe(restored, controller, clock, 1, 100)
    fill(restored, controller, key, 1, 90)
    assert restored.account.turnover == 180 and restored.account.inventory == 2
    assert controller.broker.state.confirmations[key].cumulative_fee == D('.18')
    controller.store.close()


def test_long_sequence_rebuild_restart_bounded_checkpoint_and_nav(tmp_path):
    from factorlab.trading.execution import execution_candidate

    path = tmp_path/'paper.db'
    config = GridConfig(mode='atr', lower=80, upper=120, levels=4, initial_inventory=0,
        initial_cash=1000, quantity=1, max_inventory=4, atr_window=2, atr_multiplier=1,
        rebuild_threshold=1, min_rebuild_interval=1, fee_bps=10, slippage_bps=0,
        max_turnover=100, max_drawdown=1, breakout='rebuild')
    grid, controller, clock = setup(path, config=config)
    checkpoint_sizes = []
    for n in range(64):
        if n == 32:
            controller.store.close()
            grid, controller, clock = setup(path, config=config)
            grid.restore(controller)
            assert not grid.account.reconciled
            assert controller.reconcile().reconciled
        clock[0] = NOW + timedelta(days=n)
        price = [100, 90, 100, 110, 100, 95, 105, 100][n % 8]
        prior = list(controller.broker.state.orders.values())
        observation = dict(id=str(n), time=clock[0].replace(tzinfo=None).isoformat(), price=price,
            volume=20, completed_bar=True, high=price+2, low=price-2, close=price)
        grid.observe_paper(controller, observation, delivery=grid.delivery+1, observed_at=clock[0])
        for record in prior:
            if record.snapshot.status in {S.FILLED, S.CANCELED, S.REJECTED}:
                continue
            candidate = execution_candidate(record.snapshot, ask=D(price), bid=D(price), budget=D(1),
                slippage=D(0), step=grid.rules.step_size)
            if candidate is not None:
                quantity, execution_price = candidate
                fill(grid, controller, record.snapshot.intent.client_order_id, quantity, execution_price)
        for key in sorted(grid.cancellations):
            grid.cancel_paper(controller, key, delivery=grid.delivery+1)
        if grid.pending_bar is not None:
            grid.complete_bar_paper(controller, delivery=grid.delivery+1)
        for key, order in list(grid.orders.items()):
            if order.status == S.NEW:
                grid.submit_paper(controller, key, delivery=grid.delivery+1)
        checkpoint = controller.broker.state.runtime['grid_execution']['checkpoint']
        checkpoint_sizes.append(len(dumps(checkpoint)))
        assert len(checkpoint['true_ranges']) <= 2
        assert 'commands' not in checkpoint
    assert grid.rebuilds >= 4
    assert max(checkpoint_sizes) < 12000
    quantities = {}
    turnover = signed_notional = fees = inventory = D(0)
    fills = 0
    for _, event in controller.store.order_events():
        delta = event.cumulative_filled_quantity - quantities.get(event.client_order_id, D(0))
        quantities[event.client_order_id] = event.cumulative_filled_quantity
        if delta:
            fills += 1
            intent = controller.broker.state.orders[event.client_order_id].snapshot.intent
            sign = 1 if intent.side == Side.BUY else -1
            turnover += delta * event.fill_price
            signed_notional += sign * delta * event.fill_price
            inventory += sign * delta
    for record in controller.broker.state.orders.values():
        fees += record.snapshot.cumulative_fee
    assert fills >= 4
    assert grid.account.turnover == turnover
    assert grid.account.inventory == inventory
    assert controller.broker.state.cash['USD'] == 1000 - signed_notional - fees
    assert grid.account.equity == 1000 - signed_notional - fees + inventory * price
    controller.store.close()


def test_cancel_restore_unknown_and_terminal_conflict(tmp_path):
    path = tmp_path/'paper.db'
    grid, controller, clock = setup(path)
    observe(grid, controller, clock, 0, 100)
    key = submit(grid, controller)
    fill(grid, controller, key, 1, 90)
    controller.store.close()
    restored, controller, clock = setup(path)
    restored.restore(controller)
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        observe(restored, controller, clock, 1, 100)
    assert controller.broker.state.sequence == 2
    assert controller.reconcile().reconciled
    observe(restored, controller, clock, 1, 70)
    canceled = restored.cancel_paper(controller, key, delivery=restored.delivery+1)
    assert canceled.status == S.CANCELED and controller.account().assets[0].reserved == 0
    before = controller.store.load()
    conflict = replace(canceled, event_id='late-full', status=S.FILLED,
        cumulative_filled_quantity=D(2), cumulative_fee=D('.18'), fill_price=D(90))
    with pytest.raises(GridConfirmationConflict):
        restored.confirm_paper(controller, conflict, delivery=restored.delivery+1)
    assert controller.broker.state.cash == before.cash
    assert controller.broker.state.positions == before.positions
    assert restored.conflict == 'late-full'
    controller.store.close()
