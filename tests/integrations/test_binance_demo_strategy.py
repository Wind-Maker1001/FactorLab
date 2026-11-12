from datetime import timedelta
from decimal import Decimal as D

from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading.strategy import ClosedBar, DualMAStrategy
from factorlab.trading.paper import Quote
from test_binance_demo_execution import setup
import pytest
from tests.trading.test_frozen_strategy import bundle, strategy as frozen_strategy
from factorlab.trading.frozen_strategy import FixedScoreConversion


from factorlab.trading.storage import dumps


def log_execution(runner, controller, fake):
    state = controller.broker.state
    print('OFFLINE_DEMO_EXECUTION ' + dumps(dict(
        strategy=runner.strategy.describe(), signal=state.runtime['last_signal'],
        target=state.runtime['last_order'], orders=[r.snapshot for r in state.orders.values()],
        confirmations=list(state.confirmations.values()), account=controller.account(),
        ledger=[row[0] for row in controller.store.db.execute(
            "SELECT payload FROM events WHERE id NOT LIKE 'signal:%' ORDER BY rowid")],
        protocol=[dict(method=m, path=p) for m, p, _ in fake.calls])))


def runtime(strategy=None, protocol=':memory:', paper=':memory:', fake=None):
    adapter, gateway, controller, fake = setup(protocol, paper, fake)
    runner = DemoStrategyRuntime(controller, strategy or DualMAStrategy())
    runner.recover()
    start = NOW - timedelta(hours=21)
    for i in range(21):
        p = D(100 if i < 20 else 110)
        opened = start + timedelta(hours=i)
        bar = ClosedBar('NVDAUSDT', opened, opened + timedelta(hours=1),
                        p, p, p, p, D(100), 3600, 'BINANCE_USDM_PUBLIC_REST')
        runner.closed_bar(bar)
    return runner, adapter, controller, fake, bar


def test_dual_ma_demo_execution_and_ledger():
    runner, adapter, controller, fake, bar = runtime()
    assert not runner.closed_bar(bar)
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=1)
    event = runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=1)))
    assert event is not None
    key = event.client_order_id
    qty = controller.broker.state.orders[key].snapshot.intent.quantity
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(key, str(qty))))
    runner.recover()
    assert controller.account().positions[0].quantity == qty
    assert controller.account().assets[0].total == D(1000) - qty * D('.1')
    log_execution(runner, controller, fake)
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1


@pytest.mark.parametrize('bundle', ['ridge'], indirect=True)
def test_frozen_ridge_actual_demo_execution(bundle):
    strategy = frozen_strategy(bundle, conversion=FixedScoreConversion(0., -.01, D('.1')))
    runner, adapter, controller, fake, bar = runtime(strategy)
    assert controller.broker.state.runtime['last_signal']['model_id'] == 'ridge'
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=1)
    event = runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=1)))
    assert event is not None
    key = event.client_order_id
    quantity = controller.broker.state.orders[key].snapshot.intent.quantity
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(key, str(quantity))))
    assert runner.recover().reconciled
    assert controller.account().positions[0].quantity == quantity
    assert controller.broker.state.runtime['last_signal']['model_bundle_id'] == bundle.metadata['content_sha256']
    log_execution(runner, controller, fake)


def test_unknown_queries_without_resubmit():
    runner, adapter, controller, fake, bar = runtime()
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=1)
    fake.timeout_next = True
    assert runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=1))) is None
    assert next(iter(controller.broker.state.orders.values())).snapshot.reconcile_required
    assert runner.recover().reconciled
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1
    assert not runner.closed_bar(bar)


def test_partial_restart_and_external_account_change(tmp_path):
    protocol, paper = tmp_path / 'protocol', tmp_path / 'account'
    runner, adapter, controller, fake, bar = runtime(protocol=protocol, paper=paper)
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=1)
    event = runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=1)))
    key = event.client_order_id
    message = fake.trade_update(key, '.25')
    controller.replay(UserDataEvents(adapter).parse(message))
    assert not controller.replay(UserDataEvents(adapter).parse(message))
    quantity = controller.broker.state.orders[key].snapshot.intent.quantity
    fake.trade_update(key, str(quantity - D('.25')))
    adapter.store.close()
    controller.store.close()
    adapter, gateway, controller, fake = setup(protocol, paper, fake, now=NOW + timedelta(milliseconds=1))
    runner = DemoStrategyRuntime(controller, DualMAStrategy())
    assert runner.recover().reconciled
    assert controller.account().positions[0].quantity == quantity
    assert not runner.closed_bar(bar)
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1
    fake.cash += D(17)
    fake.quantity += D('.5')
    assert runner.recover().reconciled
    assert controller.account().assets[0].total == fake.cash
    assert controller.account().positions[0].quantity == fake.quantity


def test_actual_fee_not_local_estimate():
    runner, adapter, controller, fake, bar = runtime()
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=1)
    event = runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=1)))
    message = fake.trade_update(event.client_order_id, '.25')
    message['o']['n'] = '.0125'
    fake.trades[-1]['commission'] = '.0125'
    fake.cash += D('.0125')
    controller.replay(UserDataEvents(adapter).parse(message))
    assert controller.broker.state.cash['USDT'] == D('999.9875')
    assert runner.recover().reconciled


def test_confirmed_buy_then_zero_target_without_rest_refresh():
    runner, adapter, controller, fake, _ = runtime()
    now = NOW + timedelta(milliseconds=1)
    controller.clock = controller.broker.clock = lambda: now
    event = runner.quote(Quote('NVDAUSDT', D(100), now))
    qty = controller.broker.state.orders[event.client_order_id].snapshot.intent.quantity
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(event.client_order_id, str(qty))))
    assert not adapter.facts.shared().positions
    reads = len([c for c in fake.calls if c[0] == 'GET'])
    pending = dict(controller.broker.state.runtime['last_signal'],
                   signal_id='close-confirmed-buy', direction='SELL', target_weight=D(0))
    controller._atomic(lambda: controller.broker.state.runtime.update(pending=pending))
    close = runner.quote(Quote('NVDAUSDT', D(100), now))
    assert controller.broker.state.runtime['target_position']['current'] == qty
    assert close is not None
    intent = controller.broker.state.orders[close.client_order_id].snapshot.intent
    assert intent.side.value == 'SELL' and intent.quantity == qty and intent.reduce_only
    assert len([c for c in fake.calls if c[0] == 'GET']) == reads
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(close.client_order_id, str(qty))))
    assert not controller.account().positions
    assert controller.account().assets[0].total == fake.cash
    assert runner.quote(Quote('NVDAUSDT', D(100), now)) is None
    controller._atomic(lambda: controller.broker.state.runtime.update(
        pending=dict(pending, signal_id='already-flat')))
    assert runner.quote(Quote('NVDAUSDT', D(100), now)) is None
    assert controller.broker.state.runtime['last_order']['kind'] == 'TARGET_ALREADY_MET'
    assert len([c for c in fake.calls if c[0] == 'POST']) == 2


def test_unbooked_confirmation_blocks_zero_delta_signal():
    runner, adapter, controller, fake, _ = runtime()
    now = NOW + timedelta(milliseconds=1)
    controller.clock = controller.broker.clock = lambda: now
    event = runner.quote(Quote('NVDAUSDT', D(100), now))
    qty = controller.broker.state.orders[event.client_order_id].snapshot.intent.quantity
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(event.client_order_id, str(qty))))
    controller._atomic(lambda: controller.broker.state.orders.clear())
    pending = dict(controller.broker.state.runtime['last_signal'],
                   signal_id='unbooked-zero', target_weight=D(0), direction='SELL')
    controller._atomic(lambda: controller.broker.state.runtime.update(pending=pending))
    assert not controller.account().reconciled
    assert runner.quote(Quote('NVDAUSDT', D(100), now)) is None
    assert controller.broker.state.runtime['pending'] == pending
    assert controller.broker.state.runtime['last_rejection'] == 'RECONCILE_REQUIRED'
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1


def test_partial_confirmations_size_remaining_target(tmp_path):
    protocol, paper = tmp_path / 'protocol', tmp_path / 'account'
    runner, adapter, controller, fake, _ = runtime(protocol=protocol, paper=paper)
    now = NOW + timedelta(milliseconds=2)
    controller.clock = controller.broker.clock = lambda: now
    first = runner.quote(Quote('NVDAUSDT', D(100), now))
    for quantity in ('.2', '.3'):
        message = fake.trade_update(first.client_order_id, quantity)
        controller.replay(UserDataEvents(adapter).parse(message))
        assert not controller.replay(UserDataEvents(adapter).parse(message))
    assert controller.account().positions[0].quantity == D('.5')
    controller.cancel(first.client_order_id)
    adapter.store.close()
    controller.store.close()
    adapter, gateway, controller, fake = setup(protocol, paper, fake, now=now)
    runner = DemoStrategyRuntime(controller, DualMAStrategy())
    assert runner.recover().reconciled
    assert controller.account().positions[0].quantity == D('.5')
    pending = dict(gateway.state.runtime['last_signal'], signal_id='remaining-target')
    controller._atomic(lambda: gateway.state.runtime.update(pending=pending))
    event = runner.quote(Quote('NVDAUSDT', D(100), now))
    target = gateway.state.runtime['target_position']
    assert target['current'] == D('.5')
    remaining = gateway.state.orders[event.client_order_id].snapshot.intent.quantity
    assert remaining == target['quantity'] - D('.5')
    controller.replay(UserDataEvents(adapter).parse(fake.trade_update(event.client_order_id, str(remaining))))
    assert controller.account().positions[0].quantity == target['quantity']
    assert controller.account().assets[0].total == fake.cash
    assert runner.quote(Quote('NVDAUSDT', D(100), now)) is None
    assert len([c for c in fake.calls if c[0] == 'POST']) == 2


@pytest.mark.parametrize('condition', ['disconnect', 'unknown', 'kill'])
def test_blocked_projection_keeps_signal(condition):
    runner, adapter, controller, fake, _ = runtime()
    if condition == 'disconnect':
        runner.disconnect()
    elif condition == 'kill':
        runner.kill()
    else:
        fake.timeout_next = True
        now = NOW + timedelta(milliseconds=1)
        controller.clock = controller.broker.clock = lambda: now
        runner.quote(Quote('NVDAUSDT', D(100), now))
        pending = dict(controller.broker.state.runtime['last_signal'], signal_id='after-unknown')
        controller._atomic(lambda: controller.broker.state.runtime.update(pending=pending))
    controller.clock = controller.broker.clock = lambda: NOW + timedelta(milliseconds=2)
    pending = dict(controller.broker.state.runtime['pending'])
    before = len([c for c in fake.calls if c[0] == 'POST'])
    assert runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(milliseconds=2))) is None
    assert controller.broker.state.runtime['pending'] == pending
    assert len([c for c in fake.calls if c[0] == 'POST']) == before


def test_kill_never_dispatches_strategy_order():
    runner, adapter, controller, fake, bar = runtime()
    runner.kill()
    assert runner.quote(Quote('NVDAUSDT', D(100), NOW)) is None
    assert not [c for c in fake.calls if c[0] != 'GET']
