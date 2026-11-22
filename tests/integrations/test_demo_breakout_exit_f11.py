"""Offline deterministic regressions for P7-M19-F11: BREAKOUT auto-exit and post-flat Grid rotation."""
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
import pickle
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import demo_cloud_runtime as cloud
from factorlab.integrations.binance_futures_trading import demo_grid_supervisor as grid_mod
from factorlab.integrations.binance_futures_trading import demo_runtime as runtime_mod
from factorlab.integrations.binance_futures_trading import network, protocol
from factorlab.integrations.binance_futures_trading.demo_cli import open_session, parse_args
from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import fifo_evidence, receipt_mapping
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer, grid_factory
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused, SOURCE
from factorlab.integrations.binance_futures_trading.demo_runtime import (
    DemoRuntime, open_grid_session, saved_grid_deployment,
)
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyControl
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.integrations.binance_futures_trading.protocol import AccountReadExpired, SafetyBlocked
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market, OrderStatus as S, Side
from factorlab.trading.paper import Quote
from factorlab.trading.storage import decode
from factorlab.trading.strategy import ClosedBar
from test_binance_demo_grid_network import network_grid, observe, writes
from test_binance_demo_network import NetworkFixture, wired


SESSION_NOW = DemoSession._now


class Control:
    def __init__(self, state='RUNNING'):
        self.state = state

    def desired(self):
        return self.state


def _bar(number, *, close=D('150'), high=None, low=None, open_=None):
    closed = NOW + timedelta(minutes=number)
    open_ = close if open_ is None else open_
    high = max(open_, close) + D('1') if high is None else high
    low = min(open_, close) - D('1') if low is None else low
    return ClosedBar(
        symbol='NVDAUSDT', opened_at=closed - timedelta(minutes=1), closed_at=closed,
        open=open_, high=high, low=low, close=close, volume=D('10'),
        interval_seconds=60, source=SOURCE,
    )


def _start_continuous(root, store, *, opening_equity=D('1000'), **limits):
    with closing(DemoRuntime(root)) as runtime:
        epoch = runtime.stop_epoch()
        with open_session(root, store, runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _):
            session.recover()
            return runtime.start_continuous(
                store.config, opening_equity=opening_equity, stop_epoch=epoch, **limits,
            )


@pytest.fixture
def f11_harness(network_grid, monkeypatch):
    """Deterministic HTTP-intercepted Execute harness with controllable book, mark, klines, and faults."""
    root, store, factory, enable, fake, requests = network_grid
    state = SimpleNamespace(
        seconds=0.2,
        wall_offset=0.0,
        quote_age=100,
        bid=D('150'),
        ask=D('150'),
        mark=D('150'),
        bar_close=D('150'),
        bar_high=D('153'),
        bar_low=D('147'),
        bar_overrides={},
        fail_endpoints={},
        polls=0,
        stop_after_polls=None,
        on_report=None,
        on_request=None,
        use_dynamic_factory=False,
        custom_factory=None,
        reprice_seconds=None,
        max_attempts=None,
    )
    initial = NOW.timestamp()
    original_init = protocol.FuturesClient.__init__

    def initialize(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        client.clock = lambda: initial + state.seconds + state.wall_offset
        client.monotonic = lambda: state.seconds

    original_request = NetworkFixture.__call__

    def response(exchange, request, timeout):
        if state.on_request is not None:
            intercepted = state.on_request(exchange, request, timeout, original_request, state)
            if intercepted is not None:
                return intercepted
        for suffix, failures in list(state.fail_endpoints.items()):
            if request.path.endswith(suffix) and failures:
                action = failures.pop(0)
                if isinstance(action, Exception):
                    raise action
                return action
        if request.path.endswith('/klines'):
            exchange.calls.append((request.method, request.path, parse_qs(request.query)))
            params = parse_qs(request.query)
            start, end = int(params['startTime'][0]), int(params['endTime'][0])
            payload = []
            for at in range(start, end + 1, 60000):
                bar_spec = state.bar_overrides.get(at)
                if bar_spec is not None:
                    o, h, l, c = bar_spec
                else:
                    o, h, l, c = state.bar_close, state.bar_high, state.bar_low, state.bar_close
                payload.append([at, str(o), str(h), str(l), str(c), '10', at + 59999])
            return Response(200, {}, json.dumps(payload).encode())
        if request.path.endswith('/bookTicker'):
            exchange.calls.append((request.method, request.path, parse_qs(request.query)))
            ms = round((initial + state.seconds) * 1000) - state.quote_age
            return Response(200, {}, json.dumps(dict(
                symbol='NVDAUSDT', bidPrice=str(state.bid), askPrice=str(state.ask), time=ms,
            )).encode())
        if request.path.endswith('/premiumIndex'):
            exchange.calls.append((request.method, request.path, parse_qs(request.query)))
            ms = round((initial + state.seconds) * 1000)
            return Response(200, {}, json.dumps(dict(
                symbol='NVDAUSDT', markPrice=str(state.mark), time=ms,
            )).encode())
        result = original_request(exchange, request, timeout)
        if request.path.endswith('/time'):
            payload = json.loads(result.body)
            payload['serverTime'] = round((initial + state.seconds) * 1000)
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v3/positionRisk':
            payload = json.loads(result.body)
            for position in payload:
                position['positionAmt'] = str(exchange.quantity)
                position['entryPrice'] = str(exchange.entry)
                position['markPrice'] = str(state.mark)
                position['notional'] = str(exchange.quantity * state.mark)
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v3/account':
            payload = json.loads(result.body)
            unrealized = exchange.quantity * (state.mark - exchange.entry) if exchange.quantity else D('0')
            collateral = abs(exchange.quantity) * exchange.entry
            for asset in payload['assets']:
                if asset['asset'] == 'USDT':
                    asset['walletBalance'] = str(exchange.cash)
                    asset['unrealizedProfit'] = str(unrealized)
                    asset['marginBalance'] = str(exchange.cash + unrealized)
                    asset['availableBalance'] = str(exchange.cash + unrealized - collateral)
                    asset['initialMargin'] = str(collateral)
            for position in payload['positions']:
                position.pop('entryPrice', None)
                position['unrealizedProfit'] = str(unrealized)
            return Response(200, {}, json.dumps(payload).encode())
        if request.path == '/fapi/v1/income':
            return Response(200, {}, b'[]')
        return result

    monkeypatch.setattr(protocol.FuturesClient, '__init__', initialize)
    monkeypatch.setattr(DemoSession, '_now', SESSION_NOW)
    monkeypatch.setattr(NetworkFixture, '__call__', response)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)
    monkeypatch.setattr(grid_mod, 'detect_systemd_unit', lambda: None)
    monkeypatch.setattr(grid_mod.signal, 'signal', lambda *args: None)

    real_grid_factory = grid_mod.grid_factory

    def select_factory(settings, *, new_deployment=False):
        if state.custom_factory is not None:
            return state.custom_factory
        if state.use_dynamic_factory:
            return real_grid_factory(settings, new_deployment=new_deployment)
        return factory

    monkeypatch.setattr(grid_mod, 'grid_factory', select_factory)

    original_supervisor_run = grid_mod.DemoSupervisor.run

    def controlled_run(self, *, seconds=None):
        worker = self.feed
        orig_open = worker._open

        def patched_open():
            orig_open()
            if worker.bridge is not None:
                if state.reprice_seconds is not None:
                    worker.bridge.exit_reprice_seconds = state.reprice_seconds
                if state.max_attempts is not None:
                    worker.bridge.exit_max_attempts = state.max_attempts

        worker._open = patched_open
        if worker.bridge is not None:
            if state.reprice_seconds is not None:
                worker.bridge.exit_reprice_seconds = state.reprice_seconds
            if state.max_attempts is not None:
                worker.bridge.exit_max_attempts = state.max_attempts
        orig_report = self.report

        def wrapped_report(raw):
            orig_report(raw)
            state.polls += 1
            if state.on_report is not None:
                state.on_report(self, json.loads(raw), state)
            if state.stop_after_polls is not None and state.polls >= state.stop_after_polls:
                self.stop()

        self.report = wrapped_report
        return original_supervisor_run(self, seconds=seconds)

    monkeypatch.setattr(grid_mod.DemoSupervisor, 'run', controlled_run)

    def start_execute(*extra, poll_seconds='0.01', seconds='60', use_cli=False):
        args = parse_args([
            'grid-supervisor', 'start', '--execute',
            '--poll-seconds', str(poll_seconds),
            '--seconds', str(seconds),
            '--min-free-bytes', '0',
            *extra,
        ])
        if use_cli:
            return grid_mod.run_cli(args, root=root, credentials=store)
        return grid_mod.run(args, root=root, credentials=store)

    return SimpleNamespace(
        root=root, store=store, factory=factory, enable=enable,
        fake=fake, requests=requests, state=state, start_execute=start_execute,
    )


def _setup_breakout_with_position(h, *, fill_qty='.04', fill_price='146', breakout_price='142',
                                  cancel_open_sell=True, **continuous_limits):
    """Create a continuous Grid that filled BUY, triggered PAUSED/BREAKOUT, and holds long inventory."""
    policy = _start_continuous(h.root, h.store, **continuous_limits)
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as old:
        old.recover()
        observe(old)
        buy = old.grid.active[0].intent
        h.fake.trade_update(buy.client_order_id, fill_qty, fill_price)
        old.recover()
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(),
                         price=breakout_price, volume=0), dispatch=False)
        if old.controller.account().open_orders:
            old.dispatch()
            old.recover()
        if cancel_open_sell:
            old.withdraw_unsubmitted()
        old_id = old.grid.deployment_id
        assert old.grid.state == 'PAUSED' and old.grid.reason == 'BREAKOUT'
        assert old.grid.closed_by_breakout
        assert old.account().inventory == D(fill_qty)
        assert not old.controller.account().open_orders
    h.state.bid = h.state.ask = h.state.mark = D(breakout_price)
    h.state.bar_close = D(breakout_price)
    h.state.bar_high = D(breakout_price) + D('1')
    h.state.bar_low = D(breakout_price) - D('1')
    return policy, old_id, buy


# ---------------------------------------------------------------------------
# F09 Original Scenario Deterministic Regression
# ---------------------------------------------------------------------------

def test_f11_f09_original_scenario_deterministic_regression(f11_harness):
    """Exact F09 reproduction: g-00001 [129.59, 132.21], 0.04 NVDA @ 129.59, PAUSED/BREAKOUT, 0 open, 0 UNKNOWN."""
    h = f11_harness
    h.state.use_dynamic_factory = True
    h.state.bid = h.state.ask = h.state.mark = D('130.90')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('130.90'), D('131.50'), D('130.00')
    policy = _start_continuous(
        h.root, h.store, opening_equity=D('1000'),
        total_orders=100, total_notional=D('700'), orders=4, max_loss=D('1'),
        prefix='fl-d7345f8795464a65',
    )

    # Generation 0 -> flat BREAKOUT -> rotate to fl-d7345f8795464a65-g-00001 ([129.59, 132.21]).
    with open_grid_session(h.root, h.store, grid_factory(None), continuous=True) as g0:
        g0.recover()
        g0.observe(dict(id='g0-breakout', time=NOW.replace(tzinfo=None).isoformat(), price='160', volume=0),
                   dispatch=False)
        g0.withdraw_unsubmitted()
        g1 = g0.rotate_if_terminal(g0, running=lambda: True)
        assert g1.grid.deployment_id == 'fl-d7345f8795464a65-g-00001'
        assert (g1.grid.config.lower, g1.grid.config.upper) == (129.59, 132.21)
        g1.observe(dict(id='g1-seed', time=NOW.replace(tzinfo=None).isoformat(), price='130.90', volume=0))
        buy = g1.grid.active[0].intent
        assert buy.client_order_id == 'fl-d7345f8795464a65-g-00001-00000001'
        assert buy.side is Side.BUY and buy.quantity == D('0.04') and buy.limit_price == D('129.59')
        # BUY fills at 129.59, then price breaks below 129.59 -> PAUSED / BREAKOUT with 0 open orders.
        h.fake.trade_update(buy.client_order_id, '0.04', '129.59')
        g1.recover()
        g1.observe(dict(id='g1-breakout', time=NOW.replace(tzinfo=None).isoformat(), price='128.50', volume=0),
                   dispatch=False)
        g1.withdraw_unsubmitted()
        assert g1.grid.state == 'PAUSED' and g1.grid.reason == 'BREAKOUT'
        assert g1.account().inventory == D('0.04')
        assert not g1.session.status()['open_orders']
        assert not g1.session.status()['unknown']

    # Market is now at bid=128.50, ask=128.60, mark=128.55 (below range, legal notional 5.14 >= 5).
    h.state.bid, h.state.ask, h.state.mark = D('128.50'), D('128.60'), D('128.55')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('128.55'), D('128.80'), D('128.40')
    seen_phases = []
    exit_order_id = None

    def track(supervisor, raw, state):
        nonlocal exit_order_id
        worker = supervisor.feed
        snap = worker.health_snapshot()
        grid_snap = snap['grid']
        bexit = grid_snap['breakout_exit']
        seen_phases.append((grid_snap['deployment_id'], grid_snap['state'], bexit['phase'] if bexit else None))
        if state.polls == 1:
            # Poll 1 immediately submits reduce-only SELL without waiting for a new closed bar!
            assert grid_snap['deployment_id'] == 'fl-d7345f8795464a65-g-00001'
            assert bexit['phase'] == 'EXIT_WORKING'
            assert bexit['entry_blocked'] is True
            exit_order_id = bexit['active_exit_order_id']
            assert exit_order_id.startswith('fl-d7345f8795464a65-g-00001-')
            assert len(writes(h.requests, 'POST')) == 2
            # Must NOT rotate while position is still open!
            assert worker.bridge.generation == 1
        elif state.polls == 2:
            # Still unfilled -> stays EXIT_WORKING, no duplicate POST, no premature rotation.
            assert grid_snap['deployment_id'] == 'fl-d7345f8795464a65-g-00001'
            assert bexit['phase'] == 'EXIT_WORKING'
            assert len(writes(h.requests, 'POST')) == 2
            # Simulate real exchange fill of the exit SELL at 128.50.
            h.fake.trade_update(exit_order_id, '0.04', '128.50')
            state.seconds += 1.0
        elif state.polls == 3:
            # Official GET confirmed flat -> rotated cleanly to fl-d7345f8795464a65-g-00002!
            assert grid_snap['deployment_id'] == 'fl-d7345f8795464a65-g-00002'
            assert grid_snap['state'] == 'RUNNING'
            assert bexit is None
            assert worker.bridge.feed_deployment_id == 'fl-d7345f8795464a65-g'
            assert worker.bridge.generation == 2

    h.state.on_report = track
    h.state.stop_after_polls = 3
    assert h.start_execute() == 0
    assert seen_phases == [
        ('fl-d7345f8795464a65-g-00001', 'PAUSED', 'EXIT_WORKING'),
        ('fl-d7345f8795464a65-g-00001', 'PAUSED', 'EXIT_WORKING'),
        ('fl-d7345f8795464a65-g-00002', 'RUNNING', None),
    ]
    post_list = writes(h.requests, 'POST')
    assert len(post_list) == 2
    sell_post = post_list[1]
    assert sell_post['side'] == ['SELL'] and sell_post['type'] == ['LIMIT'] and sell_post['timeInForce'] == ['GTC']
    assert sell_post['reduceOnly'] == ['true'] and sell_post['positionSide'] == ['BOTH']
    assert sell_post['quantity'] == ['0.04'] and D(sell_post['price'][0]) == D('128.50')
    # Verify original g-00001 range stayed immutable & FIFO reconciled with zero residual.
    with open_grid_session(h.root, h.store, read_only=True) as inspect:
        inspect.recover()
        g1_saved = decode(json.loads(inspect.controller.store.event_payload(
            'demo-grid:fl-d7345f8795464a65-g-00001:deployment')))
        assert (g1_saved['config']['lower'], g1_saved['config']['upper']) == (129.59, 132.21)
        fifo = fifo_evidence(inspect.session, D('1000'))
        assert fifo['attribution']['summary']['reconciliation_residual'] == 0
        assert fifo['attribution']['summary']['residual_inventory'] == 0


# ---------------------------------------------------------------------------
# 1. BREAKOUT + Flat -> continues original F08 rotation
# ---------------------------------------------------------------------------

def test_f11_01_breakout_flat_continues_f08_rotation(f11_harness):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _start_continuous(h.root, h.store)
    with open_grid_session(h.root, h.store, grid_factory(None), continuous=True) as old:
        old.recover()
        old.observe(dict(id='initial', time=NOW.replace(tzinfo=None).isoformat(), price='150', volume=0))
        old_id = old.grid.deployment_id
        old.observe(dict(id='breakout', time=NOW.replace(tzinfo=None).isoformat(), price='155', volume=0),
                    dispatch=False)
        assert old.grid.closed_by_breakout
        assert old.account().inventory == 0

    h.state.bid = h.state.ask = h.state.mark = D('155')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('155'), D('156'), D('154')
    trace = []

    def track(supervisor, raw, state):
        worker = supervisor.feed
        trace.append((worker.bridge.grid.deployment_id, worker.bridge.generation))
        state.seconds += 1.0

    h.state.on_report = track
    h.state.stop_after_polls = 2
    assert h.start_execute() == 0
    # Poll 1 cancels stale open BUY; Poll 2 confirms flat & rotates to g-00001 without any SELL POST!
    assert trace == [(old_id, 0), (old_id + '-00001', 1)]
    assert len(writes(h.requests, 'DELETE')) == 1
    assert [p['side'][0] for p in writes(h.requests, 'POST')] == ['BUY']


# ---------------------------------------------------------------------------
# 2 & 3. BREAKOUT + Position -> blocks BUY, submits legal reduce-only EXIT & GET receipt
# ---------------------------------------------------------------------------

def test_f11_02_03_breakout_blocks_buy_submits_exit_and_verifies_official_get_receipt(f11_harness):
    h = f11_harness
    policy, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('142.93'), D('143.05'), D('143.00')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('143.00'), D('143.50'), D('142.50')

    # Advance across a bar boundary so closed_bar ALSO fires while in BREAKOUT!
    h.state.seconds = 60.2
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0

    post_list = writes(h.requests, 'POST')
    assert len(post_list) == 2
    assert post_list[0]['side'] == ['BUY']
    exit_post = post_list[1]
    exit_id = exit_post['newClientOrderId'][0]
    assert exit_id.startswith(old_id + '-')
    assert exit_post['symbol'] == ['NVDAUSDT']
    assert exit_post['side'] == ['SELL']
    assert exit_post['type'] == ['LIMIT']
    assert exit_post['timeInForce'] == ['GTC']
    assert exit_post['positionSide'] == ['BOTH']
    assert exit_post['reduceOnly'] == ['true']
    assert exit_post['quantity'] == ['0.04']
    assert D(exit_post['price'][0]) == D('142.93')
    # Official GET /fapi/v1/order readback happened right after POST (get_confirmed_writes=True).
    post_indices = [i for i, (m, p, _) in enumerate(h.fake.calls) if m == 'POST' and p == '/fapi/v1/order']
    assert any(m == 'GET' and p == '/fapi/v1/order' and q.get('origClientOrderId') == [exit_id]
               for m, p, q in h.fake.calls[post_indices[-1] + 1:])
    # Verify persisted receipt mapping and runtime dispatch record.
    with open_grid_session(h.root, h.store, read_only=True) as inspect:
        inspect.recover()
        mapping = {row['client_order_id']: row for row in receipt_mapping(inspect.session.adapter)}
        assert mapping[exit_id]['receipt_source'] == 'GET'
        assert mapping[exit_id]['order_query']['status'] == 'NEW'
        assert mapping[exit_id]['order_query']['side'] == 'SELL'
        status = inspect.breakout_exit_status()
        assert status['phase'] == 'EXIT_WORKING'
        assert status['entry_blocked'] is True
        assert status['active_exit_order_id'] == exit_id
    with closing(DemoRuntime(h.root)) as runtime:
        consumed = runtime.consumption(policy)
        assert consumed['posts'] == 2
        assert D(consumed['submitted_notional']) == D('0.04') * D('146') + D('0.04') * D('142.93')


# ---------------------------------------------------------------------------
# 4. Unfilled exit SELL -> monitors, no fake fills, bounded reprice & attempt cap
# ---------------------------------------------------------------------------

def test_f11_04_unfilled_exit_monitors_and_reprices_bounded(f11_harness):
    from factorlab.integrations.binance_futures_trading.demo_grid_bridge import EXIT_MAX_ATTEMPTS, EXIT_REPRICE_SECONDS
    assert EXIT_REPRICE_SECONDS == 180 and EXIT_MAX_ATTEMPTS == 5
    h = f11_harness
    policy, old_id, _ = _setup_breakout_with_position(h, breakout_price='143', total_orders=20, orders=10)
    h.state.reprice_seconds = 10
    h.state.max_attempts = 2
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')

    def track(supervisor, raw, state):
        worker = supervisor.feed
        if state.polls == 1:
            # Exit #1 placed at 143.00.
            assert len(writes(h.requests, 'POST')) == 2
            assert not writes(h.requests, 'DELETE')
            # Move price DOWN below limit price (crossing!), but within cooldown (5s < 10s):
            # MUST NOT fake fill, MUST NOT cancel prematurely, MUST NOT post duplicate!
            state.seconds += 5.0
            state.bid, state.ask, state.mark = D('141.00'), D('141.10'), D('141.05')
        elif state.polls == 2:
            assert worker.bridge.account().inventory == D('0.04')
            assert len(writes(h.requests, 'POST')) == 2
            assert not writes(h.requests, 'DELETE')
            # Advance past cooldown (>= 10s, staying within 30s R13 quote gap): triggers Phase-1 DELETE only!
            state.seconds += 10.0
        elif state.polls == 3:
            assert len(writes(h.requests, 'DELETE')) == 1
            assert len(writes(h.requests, 'POST')) == 2
            assert worker.bridge.breakout_exit_status()['phase'] == 'EXIT_RECONCILING'
            state.seconds += 1.0
        elif state.polls == 4:
            # Next poll GET confirms CANCELED -> submits bounded replacement Exit #2 at 141.00!
            assert len(writes(h.requests, 'POST')) == 3
            assert D(writes(h.requests, 'POST')[2]['price'][0]) == D('141.00')
            assert worker.bridge.breakout_exit_status()['attempts'] == 2
            # Drop price further & advance past cooldown again -> attempt cap (2) forbids 3rd cancel/post!
            state.seconds += 15.0
            state.bid, state.ask, state.mark = D('139.00'), D('139.10'), D('139.05')
        elif state.polls == 5:
            assert len(writes(h.requests, 'DELETE')) == 1
            assert len(writes(h.requests, 'POST')) == 3
            assert worker.bridge.breakout_exit_status()['phase'] == 'EXIT_WORKING'

    h.state.on_report = track
    h.state.stop_after_polls = 5
    assert h.start_execute() == 0
    with closing(DemoRuntime(h.root)) as runtime:
        consumed = runtime.consumption(policy)
        # Both exit attempts are counted in cumulative budget!
        assert consumed['posts'] == 3
        assert D(consumed['submitted_notional']) == D('0.04') * (D('146') + D('143') + D('141'))


# ---------------------------------------------------------------------------
# P2. Replacement admission before DELETE and again before POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('blocked', ['price_band', 'missing_mark', 'stale_mark', 'future_mark',
                                    'quote_band', 'missing_rule', 'replayed_cancel'])
def test_f11_reprice_preserves_working_sell_until_band_recovers(f11_harness, blocked):
    """P2: a valid 143 SELL must survive an inadmissible 140 replacement."""
    h = f11_harness
    _setup_breakout_with_position(h, breakout_price='143')
    with ExitStack() as stack:
        bridge = stack.enter_context(open_grid_session(h.root, h.store, h.factory, continuous=True))
        bridge.recover()
        original = bridge.step_breakout_exit()
        key = original.intent.client_order_id
        assert original.intent.limit_price == D('143')
        h.state.seconds += 181
        h.state.bid, h.state.ask, h.state.mark = D('140'), D('145.10'), D('145')
        bridge.recover()
        if blocked == 'replayed_cancel':
            bridge._command('exit_cancel', key)
            stack.close()
            bridge = stack.enter_context(open_grid_session(h.root, h.store, h.factory, continuous=True))
            bridge.recover()
        if blocked == 'quote_band':
            h.state.bid, h.state.ask, h.state.mark = D('141'), D('146'), D('141')
        band = bridge.gateway.rules.filters['PERCENT_PRICE']
        down = band['multiplierDown']
        assert D(down) == D('.97') and D('145') * D(down) == D('140.65')
        if blocked == 'missing_rule':
            band.pop('multiplierDown')

        marks = []

        def mark_response(exchange, request, timeout, original_request, state):
            if request.path.endswith('/premiumIndex'):
                marks.append(request)
                ms = round((NOW.timestamp() + state.seconds) * 1000)
                payload = dict(symbol='NVDAUSDT', markPrice=str(state.mark), time=ms)
                if blocked == 'missing_mark':
                    payload.pop('markPrice')
                elif blocked == 'stale_mark':
                    payload['time'] -= 5001
                elif blocked == 'future_mark':
                    payload['time'] += 1
                return Response(200, {}, json.dumps(payload).encode())
            return None

        h.state.on_request = mark_response
        assert bridge.step_breakout_exit(book=bridge.session._fresh_book()) is None
        assert len(marks) == 1
        assert not writes(h.requests, 'DELETE')
        assert len(writes(h.requests, 'POST')) == 2
        assert bridge.session.adapter.order(key).status is S.SUBMITTED
        assert bridge.breakout_exit_status()['active_exit_order_id'] == key
        assert bridge.exit_block_reason == ('EXIT_MARK_UNAVAILABLE' if blocked in (
            'missing_mark', 'stale_mark', 'future_mark') else 'EXIT_PRICE_BAND')
        assert (key in bridge.grid.exit_cancellations) is (blocked == 'replayed_cancel')

        # New market evidence permits one DELETE, GET reconciliation, then one POST.
        h.state.on_request = None
        band['multiplierDown'] = down
        h.state.bid, h.state.ask, h.state.mark = D('141'), D('141.10'), D('141.05')
        bridge.recover()
        mark_gets = lambda: sum(m == 'GET' and p.endswith('/premiumIndex') for m, p, _ in h.fake.calls)
        before_cancel = mark_gets()
        assert bridge.step_breakout_exit().status is S.CANCELED
        assert mark_gets() == before_cancel + 1
        assert len(writes(h.requests, 'DELETE')) == 1
        assert len(writes(h.requests, 'POST')) == 2
        assert bridge.step_breakout_exit() is None
        assert mark_gets() == before_cancel + 1
        bridge.recover()
        before_post = mark_gets()
        replacement = bridge.step_breakout_exit()
        assert mark_gets() == before_post + 1
        assert replacement.intent.limit_price == D('141')
        assert replacement.intent.quantity == D('.04')
        assert replacement.intent.reduce_only
        assert len(writes(h.requests, 'DELETE')) == 1
        assert len(writes(h.requests, 'POST')) == 3


def test_f11_replacement_post_rechecks_market_after_cancel(f11_harness, monkeypatch):
    h = f11_harness
    _setup_breakout_with_position(h, breakout_price='143')
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        bridge.step_breakout_exit()
        h.state.seconds += 181
        h.state.bid, h.state.ask, h.state.mark = D('141'), D('141.10'), D('141.05')
        bridge.recover()
        assert bridge.step_breakout_exit().status is S.CANCELED
        bridge.recover()
        submit = bridge.session.submit_intent

        def changed_market(intent, **kwargs):
            h.state.mark = D('150')  # Even 141 is now outside the formal POST band.
            return submit(intent, **kwargs)

        monkeypatch.setattr(bridge.session, 'submit_intent', changed_market)
        assert bridge.step_breakout_exit() is None
        assert bridge.exit_block_reason == 'EXIT_PRICE_BAND'
        assert len(writes(h.requests, 'POST')) == 2
        assert len(writes(h.requests, 'DELETE')) == 1
        assert bridge.account().inventory == D('.04')
        assert bridge._dispatched_exit_attempts() == 1
        monkeypatch.setattr(bridge.session, 'submit_intent', submit)
        h.state.mark = D('141.05')
        bridge.recover()
        assert bridge.step_breakout_exit().intent.limit_price == D('141')
        assert len(writes(h.requests, 'POST')) == 3
        assert len(writes(h.requests, 'DELETE')) == 1


@pytest.mark.parametrize('stale', ['account', 'book'])
def test_f11_exit_still_requires_fresh_account_and_book(f11_harness, stale):
    h = f11_harness
    _setup_breakout_with_position(h, breakout_price='143')
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        consumer = GridConsumer(bridge, Control())
        if stale == 'account':
            h.state.seconds += 5.001
        else:
            h.state.quote_age = 5001
        raw = bridge.session.client.request('GET', '/fapi/v1/ticker/bookTicker',
                                            {'symbol': 'NVDAUSDT'}, signed=False)
        consumer.set_latest_book(raw)
        with pytest.raises(AccountReadExpired if stale == 'account' else SafetyBlocked,
                           match='STALE_ACCOUNT' if stale == 'account' else 'STALE_QUOTE'):
            bridge.step_breakout_exit(book=consumer.latest_book)
        assert len(writes(h.requests, 'POST')) == 1
        assert not writes(h.requests, 'DELETE')


# ---------------------------------------------------------------------------
# 5. Partial fill -> dust protection while open & legal remainder sizing after cancel
# ---------------------------------------------------------------------------

def test_f11_05a_exit_partial_fill_dust_protected_until_full_fill(f11_harness):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.reprice_seconds = 10
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    exit_id = None

    def track(supervisor, raw, state):
        nonlocal exit_id
        worker = supervisor.feed
        if state.polls == 1:
            exit_id = worker.bridge.breakout_exit_status()['active_exit_order_id']
            # Partial fill 0.02 -> remaining 0.02 @ 140 = 2.80 < 5 MIN_NOTIONAL!
            h.fake.trade_update(exit_id, '0.02', '143.00')
            state.bid, state.ask, state.mark = D('140.00'), D('140.10'), D('140.05')
            state.seconds += 15.0
        elif state.polls == 2:
            # Must NOT cancel the partially filled order (would strand 0.02 dust!), no extra POST, no rotation!
            assert worker.bridge.grid.deployment_id == old_id
            assert worker.bridge.account().inventory == D('0.02')
            assert worker.bridge.breakout_exit_status()['phase'] == 'EXIT_WORKING'
            assert len(writes(h.requests, 'POST')) == 2
            assert not writes(h.requests, 'DELETE')
            # Remaining 0.02 fills on the resting order!
            h.fake.trade_update(exit_id, '0.02', '143.00')
            state.seconds += 1.0
        elif state.polls == 3:
            # Flat confirmed -> rotated!
            assert worker.bridge.grid.deployment_id == old_id + '-00001'
            assert worker.bridge.account().inventory == 0

    h.state.on_report = track
    h.state.stop_after_polls = 3
    assert h.start_execute() == 0
    assert len(writes(h.requests, 'POST')) == 2
    assert not writes(h.requests, 'DELETE')


def test_f11_05b_exit_partial_fill_legal_remainder_submits_only_remaining_qty(f11_harness):
    """When a partially filled exit SELL is canceled leaving legal remainder (0.03 @ 170 = 5.10 >= 5), sell ONLY 0.03."""
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='170')
    h.state.reprice_seconds = 10
    h.state.bid, h.state.ask, h.state.mark = D('170.00'), D('170.10'), D('170.05')
    first_exit = None

    def track(supervisor, raw, state):
        nonlocal first_exit
        worker = supervisor.feed
        if state.polls == 1:
            first_exit = worker.bridge.breakout_exit_status()['active_exit_order_id']
            # Partial fill 0.01 -> remaining 0.03 @ 168 = 5.04 >= 5 and <= 7!
            h.fake.trade_update(first_exit, '0.01', '170.00')
            state.bid, state.ask, state.mark = D('168.00'), D('168.10'), D('168.05')
            state.seconds += 15.0
        elif state.polls == 2:
            # Triggers cancel of first_exit to reprice remaining 0.03.
            assert len(writes(h.requests, 'DELETE')) == 1
            assert len(writes(h.requests, 'POST')) == 2
            state.seconds += 1.0
        elif state.polls == 3:
            # Replacement SELL MUST be sized strictly to remaining 0.03 (NOT 0.04!).
            assert len(writes(h.requests, 'POST')) == 3
            second_post = writes(h.requests, 'POST')[2]
            assert second_post['side'] == ['SELL'] and second_post['reduceOnly'] == ['true']
            assert second_post['quantity'] == ['0.03'] and D(second_post['price'][0]) == D('168.00')
            second_id = second_post['newClientOrderId'][0]
            h.fake.trade_update(second_id, '0.03', '168.00')
            state.seconds += 1.0
        elif state.polls == 4:
            assert worker.bridge.grid.deployment_id == old_id + '-00001'
            assert worker.bridge.account().inventory == 0

    h.state.on_report = track
    h.state.stop_after_polls = 4
    assert h.start_execute() == 0


# ---------------------------------------------------------------------------
# 6. Full fill -> no extra sells, FLAT_CONFIRMED
# ---------------------------------------------------------------------------

def test_f11_06_exit_full_fill_never_submits_extra_sell(f11_harness):
    h = f11_harness
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.recover()
        res = bridge.step_breakout_exit()
        assert res.status is S.SUBMITTED
        exit_id = res.intent.client_order_id
        h.fake.trade_update(exit_id, '0.04', '143.00')
        bridge.recover()
        assert bridge.account().inventory == 0
        assert bridge.grid.state == 'EXITED' and bridge.grid.reason == 'BREAKOUT'
        assert bridge.breakout_exit_status()['phase'] == 'FLAT_CONFIRMED'
        assert bridge.step_breakout_exit() is None
    assert len(writes(h.requests, 'POST')) == 2


# ---------------------------------------------------------------------------
# 7. Cancel / Fill race (200 FILLED, 400 -2011, and Timeout)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('race_mode', ['http_200_filled', 'http_400_unknown_order', 'timeout'])
def test_f11_07_exit_cancel_fill_race(f11_harness, race_mode):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.reprice_seconds = 10
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    raced = {'done': False}

    def on_request(exchange, request, timeout, original, state):
        if request.method == 'DELETE' and request.path == '/fapi/v1/order' and not raced['done']:
            raced['done'] = True
            key = parse_qs(request.query)['origClientOrderId'][0]
            exchange.trade_update(key, '0.04', '143.00')
            if race_mode == 'http_200_filled':
                return Response(200, {}, json.dumps(exchange.orders[key]).encode())
            if race_mode == 'http_400_unknown_order':
                return Response(400, {}, b'{"code":-2011,"msg":"Unknown order sent."}')
            raise TimeoutError('synthetic cancel timeout after fill')
        return None

    h.state.on_request = on_request
    rotated = {'seen': False}

    def track(supervisor, raw, state):
        worker = supervisor.feed
        if state.polls == 1:
            assert len(writes(h.requests, 'POST')) == 2
            state.bid, state.ask, state.mark = D('141.00'), D('141.10'), D('141.05')
            state.seconds += 15.0
        elif state.polls >= 2:
            state.seconds += 1.0
            if worker.bridge is not None and worker.bridge.grid.deployment_id == old_id + '-00001':
                rotated['seen'] = True
                supervisor.stop()

    h.state.on_report = track
    h.state.stop_after_polls = 6
    assert h.start_execute() == 0
    assert raced['done'] is True
    assert rotated['seen'] is True
    # Zero replacement POST submitted after the cancel-fill race!
    assert len(writes(h.requests, 'POST')) == 2
    assert len(writes(h.requests, 'DELETE')) == 1
    with open_grid_session(h.root, h.store, read_only=True) as inspect:
        inspect.recover()
        fifo = fifo_evidence(inspect.session, D('1000'))
        assert fifo['attribution']['summary']['reconciliation_residual'] == 0
        assert fifo['attribution']['summary']['residual_inventory'] == 0


# ---------------------------------------------------------------------------
# 8. POST / DELETE UNKNOWN -> forbids replacement until GET recovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['POST', 'DELETE'])
def test_f11_08_post_or_delete_unknown_forbids_replacement_until_get_recovery(f11_harness, method):
    h = f11_harness
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    with open_grid_session(h.root, h.store, h.factory, continuous=True) as bridge:
        bridge.exit_reprice_seconds = 60
        bridge.recover()
        if method == 'POST':
            h.fake.timeout_next = True
            unknown_res = bridge.step_breakout_exit()
            assert unknown_res.status is S.UNKNOWN and unknown_res.reconcile_required
            assert bridge.breakout_exit_status()['phase'] == 'EXIT_RECONCILING'
            with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
                bridge.step_breakout_exit()
            assert len(writes(h.requests, 'POST')) == 2
            bridge.recover()
            bridge.ensure_campaign()
            assert bridge.breakout_exit_status()['phase'] == 'EXIT_WORKING'
            assert bridge.step_breakout_exit() is None
            assert len(writes(h.requests, 'POST')) == 2
        else:
            res = bridge.step_breakout_exit()
            assert res.status is S.SUBMITTED
            # Inject timeout on DELETE while exchange actually canceled the order.
            def drop_delete(exchange, request, timeout, original, state):
                if request.method == 'DELETE' and request.path == '/fapi/v1/order':
                    original(exchange, request, timeout)
                    raise TimeoutError('synthetic DELETE reply lost')
                return None
            h.state.on_request = drop_delete
            h.state.bid, h.state.ask, h.state.mark = D('141.00'), D('141.10'), D('141.05')
            h.state.seconds += 120.0
            bridge.recover()
            unknown_cancel = bridge.step_breakout_exit()
            assert unknown_cancel.status is S.UNKNOWN and unknown_cancel.reconcile_required
            assert bridge.breakout_exit_status()['phase'] == 'EXIT_RECONCILING'
            # Replacement MUST be forbidden while DELETE outcome is UNKNOWN!
            with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
                bridge.step_breakout_exit()
            assert len(writes(h.requests, 'POST')) == 2
            h.state.on_request = None
            # After GET recovery confirms CANCELED, legal replacement SELL #2 is permitted.
            bridge.recover()
            bridge.ensure_campaign()
            assert bridge.breakout_exit_status()['phase'] == 'EXIT_PENDING'
            replacement = bridge.step_breakout_exit()
            assert replacement.status is S.SUBMITTED
            assert replacement.intent.limit_price == D('141.00')
            assert len(writes(h.requests, 'POST')) == 3


# ---------------------------------------------------------------------------
# 9 & 10. SIGKILL recovery of existing exit order & restarts never duplicate POST
# ---------------------------------------------------------------------------

def test_f11_09_10_sigkill_and_multiple_restarts_recover_exit_without_duplicate_post(f11_harness):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')

    # Process 1 submits exit SELL, then terminates abruptly (simulating SIGKILL while working).
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    assert len(writes(h.requests, 'POST')) == 2
    exit_id = writes(h.requests, 'POST')[1]['newClientOrderId'][0]

    # Verify independent child process recovers exact exit order from SQLite with 0 duplicate POSTs!
    exchange_blob = h.root / 'f11-exchange.pickle'
    exchange_blob.write_bytes(pickle.dumps(h.fake.__dict__))
    store_blob = h.root / 'f11-store.pickle'
    store_blob.write_bytes(pickle.dumps((h.store.credential, h.store.config)))
    subprocess.run([sys.executable, '-c', '''
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json, pickle, sys
from pathlib import Path
import pytest
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]).parents[1] / 'src'))
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_grid_network import network_grid
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import grid_factory
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
root = Path(sys.argv[2])
exit_id = sys.argv[3]
with pytest.MonkeyPatch.context() as patch:
    wire = wired.__wrapped__(patch)
    _, store, _, _, fake, requests = network_grid.__wrapped__(root, patch, wire)
    later = NOW + timedelta(seconds=1)
    later_ms = MS + 1000
    orig = NetworkFixture.__call__
    def after_restart(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, json.dumps(dict(symbol='NVDAUSDT', bidPrice='143', askPrice='143.10', time=later_ms)).encode())
        return orig(self, request, timeout)
    patch.setattr(NetworkFixture, '__call__', after_restart)
    patch.setattr(DemoSession, '_now', lambda _: later)
    fake.__dict__.update(pickle.loads((root / 'f11-exchange.pickle').read_bytes()))
    store.credential, store.config = pickle.loads((root / 'f11-store.pickle').read_bytes())
    with open_grid_session(root, store, grid_factory(None), continuous=True) as bridge:
        bridge.recover()
        status = bridge.breakout_exit_status()
        assert status['phase'] == 'EXIT_WORKING'
        assert status['active_exit_order_id'] == exit_id
        assert status['attempts'] == 1
        assert bridge.step_breakout_exit() is None
    assert all(r.get_method() == 'GET' for r in requests)
''', str(Path(__file__).parent), str(h.root), exit_id], check=True)

    # Multiple additional restarts in the worker loop MUST NOT duplicate POST!
    for _ in range(2):
        h.state.polls = 0
        h.state.seconds += 2.0
        h.state.stop_after_polls = 1
        assert h.start_execute() == 0
        assert len(writes(h.requests, 'POST')) == 2
        snap = json.loads((h.root / 'cloud-health.json').read_text())
        assert snap['grid']['breakout_exit']['phase'] == 'EXIT_WORKING'
        assert snap['grid']['breakout_exit']['active_exit_order_id'] == exit_id

    # Finally fill exit order -> next restart rotates cleanly!
    h.fake.trade_update(exit_id, '0.04', '143.00')
    h.state.polls = 0
    h.state.seconds += 2.0
    h.state.stop_after_polls = 1
    assert h.start_execute() == 0
    snap = json.loads((h.root / 'cloud-health.json').read_text())
    assert snap['grid']['deployment_id'] == old_id + '-00001'
    assert len(writes(h.requests, 'POST')) == 2


# ---------------------------------------------------------------------------
# 11. STOP, HALT, PAUSED priority during exit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('control_mode', ['USER_PAUSED', 'USER_STOPPED', 'RUNTIME_STOPPED', 'ACCOUNT_HALTED', 'LOSS_HALT'])
def test_f11_11_stop_halt_paused_priority_during_exit(f11_harness, control_mode):
    h = f11_harness
    _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    before_posts = len(writes(h.requests, 'POST'))
    before_deletes = len(writes(h.requests, 'DELETE'))

    path = h.root / 'grid-supervisor.sqlite3'
    cfg = dict(mode='execute', interval='1m', source=SOURCE, max_backfill=120, settings=None)
    if control_mode == 'USER_PAUSED':
        with closing(StrategyControl(path, cfg)) as control:
            control.request('PAUSED')
    elif control_mode == 'USER_STOPPED':
        with closing(StrategyControl(path, cfg)) as control:
            control.request('STOPPED')
    elif control_mode == 'RUNTIME_STOPPED':
        with closing(DemoRuntime(h.root)) as runtime:
            runtime.stop()
    elif control_mode == 'ACCOUNT_HALTED':
        with closing(DemoRuntime(h.root)) as runtime:
            runtime.halt('manual risk halt during breakout')
    elif control_mode == 'LOSS_HALT':
        # 0.04 * (146 - 120) = 1.04 >= 1.00 max_loss -> HALTED before any exit write!
        h.state.bid = h.state.ask = h.state.mark = D('120.00')

    h.state.stop_after_polls = 2
    assert h.start_execute() == 0
    assert len(writes(h.requests, 'POST')) == before_posts
    assert len(writes(h.requests, 'DELETE')) == before_deletes

    snap = json.loads((h.root / 'cloud-health.json').read_text())
    if control_mode == 'USER_PAUSED':
        assert snap['desired'] == 'PAUSED'
        assert snap['grid']['breakout_exit']['phase'] == 'EXIT_BLOCKED'
        assert snap['grid']['breakout_exit']['reason'] == 'USER_PAUSED'
        # Explicit resume allows exit SELL to submit cleanly.
        assert grid_mod.run(parse_args(['grid-supervisor', 'resume', '--execute']),
                            root=h.root, credentials=h.store) == 0
        h.state.polls = 0
        h.state.seconds += 1.0
        h.state.stop_after_polls = 1
        assert h.start_execute() == 0
        assert len(writes(h.requests, 'POST')) == before_posts + 1
    elif control_mode in {'USER_STOPPED', 'RUNTIME_STOPPED'}:
        assert snap['status'] == 'STOPPED' and snap['desired'] == 'STOPPED'
    else:
        assert snap['status'] == 'HALTED' and snap['desired'] == 'HALTED'


# ---------------------------------------------------------------------------
# 12. Window auto-renewal & global budget continuity during exit
# ---------------------------------------------------------------------------

def test_f11_12_window_auto_renewal_and_global_budget_continuity(f11_harness, monkeypatch):
    h = f11_harness
    h.state.use_dynamic_factory = True
    # orders=1 forces a new campaign window on EVERY single POST!
    policy, old_id, _ = _setup_breakout_with_position(
        h, breakout_price='143', orders=1, total_orders=5, total_notional=D('100'),
    )
    h.state.reprice_seconds = 10
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    windows_seen = []

    def track(supervisor, raw, state):
        worker = supervisor.feed
        windows_seen.append(worker.bridge.runtime.active_campaign()['id'])
        if state.polls == 1:
            # Window 2 auto-created after BUY consumed Window 1's 1 POST -> Exit #1 placed!
            assert len(writes(h.requests, 'POST')) == 2
            state.bid, state.ask, state.mark = D('141.00'), D('141.10'), D('141.05')
            state.seconds += 15.0
        elif state.polls == 2:
            # Phase-1 DELETE of Exit #1.
            assert len(writes(h.requests, 'DELETE')) == 1
            state.seconds += 1.0
        elif state.polls == 3:
            # Window 3 auto-created -> Exit #2 placed!
            assert len(writes(h.requests, 'POST')) == 3
            exit2_id = writes(h.requests, 'POST')[2]['newClientOrderId'][0]
            h.fake.trade_update(exit2_id, '0.04', '141.00')
            state.seconds += 1.0
        elif state.polls == 4:
            # Window 4 auto-created & rotated to g-00001 under the exact same continuous authorization!
            assert worker.bridge.grid.deployment_id == old_id + '-00001'
            assert worker.bridge.runtime.continuous_authorization() == policy

    h.state.on_report = track
    h.state.stop_after_polls = 4
    assert h.start_execute() == 0
    assert h.state.polls == 4
    assert len(set(windows_seen)) == 3  # Window 2 (Exit #1), Window 3 (DELETE + Exit #2), Window 4 (Post-flat rotation)
    with closing(DemoRuntime(h.root)) as runtime:
        assert runtime.db.execute('SELECT count(*) FROM campaigns').fetchone()[0] == 4
        consumed = runtime.consumption(policy)
        assert consumed['posts'] == 3
        assert D(consumed['submitted_notional']) == D('0.04') * (D('146') + D('143') + D('141'))
        assert runtime.continuous_authorization()['opening_equity'] == policy['opening_equity']


# ---------------------------------------------------------------------------
# 13. Balance, position, fee, and FIFO consistency
# ---------------------------------------------------------------------------

def test_f11_13_balance_position_fee_and_fifo_consistency(f11_harness):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')

    def track(supervisor, raw, state):
        worker = supervisor.feed
        if state.polls == 1:
            exit_id = worker.bridge.breakout_exit_status()['active_exit_order_id']
            # Split partial fills at two prices to stress FIFO & cumulative fees.
            h.fake.trade_update(exit_id, '0.02', '143.00')
            state.seconds += 1.0
        elif state.polls == 2:
            exit_id = worker.bridge.breakout_exit_status()['active_exit_order_id']
            assert worker.bridge.account().inventory == D('0.02')
            h.fake.trade_update(exit_id, '0.02', '143.10')
            state.seconds += 1.0
        elif state.polls == 3:
            assert worker.bridge.grid.deployment_id == old_id + '-00001'

    h.state.on_report = track
    h.state.stop_after_polls = 3
    assert h.start_execute() == 0

    with open_grid_session(h.root, h.store, read_only=True) as inspect:
        account = inspect.session.recover()
        mapping = receipt_mapping(inspect.session.adapter)
        fifo = fifo_evidence(inspect.session, D('1000'))
        summary = fifo['attribution']['summary']
        expected_buy_fee = D('0.04') * D('146') * D('0.001')
        expected_sell_fee = (D('0.02') * D('143.00') + D('0.02') * D('143.10')) * D('0.001')
        expected_realized = D('0.02') * (D('143.00') - D('146')) + D('0.02') * (D('143.10') - D('146'))
        expected_wallet = D('1000') + expected_realized - expected_buy_fee - expected_sell_fee
        assert account['positions'] == []
        assert account['balances']['walletBalance'] == expected_wallet
        assert summary['residual_inventory'] == 0
        assert summary['reconciliation_residual'] == 0
        assert summary['actual_fees'] == expected_buy_fee + expected_sell_fee
        assert sum((row['actual_fee'] for row in mapping), D('0')) == summary['actual_fees']
        assert summary['final_equity'] == expected_wallet


# ---------------------------------------------------------------------------
# 14. Auto-rotation after flat inherits market watermark & bar isolation
# ---------------------------------------------------------------------------

def test_f11_14_rotation_after_flat_inherits_watermark_and_bar_isolation(f11_harness):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('143.05'), D('143.40'), D('142.80')
    h.state.seconds = 58.2
    trace = []

    def track(supervisor, raw, state):
        worker = supervisor.feed
        trace.append((worker.bridge.grid.deployment_id, worker.watermarks()[0],
                      worker.consumer.live_bar, worker.bridge.feed_deployment_id))
        if state.polls == 1:
            exit_id = worker.bridge.breakout_exit_status()['active_exit_order_id']
            h.fake.trade_update(exit_id, '0.04', '143.00')
            state.seconds = 59.2
        elif state.polls == 2:
            # Rotated before the 60s bar closes -> live_bar MUST be False, same bar watermark!
            assert worker.bridge.grid.deployment_id == old_id + '-00001'
            assert not worker.consumer.live_bar
            state.seconds = 60.4
        elif state.polls == 3:
            # Fresh bar after activated_at advances watermark by 60_000ms and submits under g-00001!
            assert worker.consumer.live_bar

    h.state.on_report = track
    h.state.stop_after_polls = 3
    assert h.start_execute() == 0
    assert [row[0] for row in trace] == [old_id, old_id + '-00001', old_id + '-00001']
    assert trace[0][1] == trace[1][1]
    assert trace[2][1] == trace[1][1] + 60000
    assert all(row[3] == old_id for row in trace)
    post_ids = [p['newClientOrderId'][0] for p in writes(h.requests, 'POST')]
    assert post_ids[0].startswith(old_id + '-')
    assert post_ids[1].startswith(old_id + '-')
    assert post_ids[2] == old_id + '-00001-00000001'


# ---------------------------------------------------------------------------
# 15. Rule, precision, price band, and budget violations safely block exit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('case', [
    'min_notional_low_price',
    'min_notional_small_qty',
    'min_quantity',
    'price_filter',
    'price_band',
    'max_notional',
    'global_posts_exhausted',
    'global_notional_exhausted',
])
def test_f11_15_rule_precision_and_budget_violations_block_safely(f11_harness, case):
    h = f11_harness
    h.state.use_dynamic_factory = True
    fill_qty = '0.02' if case == 'min_notional_small_qty' else '0.04'
    limits = dict(total_orders=10, total_notional=D('100'))
    if case == 'global_posts_exhausted':
        limits['total_orders'] = 1
    elif case == 'global_notional_exhausted':
        limits['total_notional'] = D('6')

    _, old_id, _ = _setup_breakout_with_position(h, fill_qty=fill_qty, breakout_price='143', **limits)
    before_posts = len(writes(h.requests, 'POST'))

    if case == 'min_notional_low_price':
        # 0.04 * 124 = 4.96 < 5 MIN_NOTIONAL (loss = 0.04 * 22 = 0.88 < 1.00 max_loss!)
        h.state.bid = h.state.ask = h.state.mark = D('124.00')
        expected_reason = 'EXIT_MIN_NOTIONAL'
    elif case == 'min_notional_small_qty':
        # 0.02 * 143 = 2.86 < 5 MIN_NOTIONAL
        h.state.bid = h.state.ask = h.state.mark = D('143.00')
        expected_reason = 'EXIT_MIN_NOTIONAL'
    elif case == 'min_quantity':
        # Inventory below stepSize / minQty (0.005 < 0.01)
        h.fake.quantity = D('0.005')
        h.state.bid = h.state.ask = h.state.mark = D('143.00')
        expected_reason = 'EXIT_MIN_QUANTITY'
    elif case == 'price_filter':
        # Above PRICE_FILTER maxPrice (10000) -> blocked before any POST!
        h.state.bid = h.state.ask = h.state.mark = D('10001.00')
        expected_reason = 'EXIT_PRICE_FILTER'
    elif case == 'price_band':
        # Bid far below mark * 0.97 (PERCENT_PRICE lower bound)
        h.state.mark = D('150.00')
        h.state.bid = D('140.00')
        h.state.ask = D('150.00')
        expected_reason = 'EXIT_PRICE_BAND'
    elif case == 'max_notional':
        # 0.04 * 180 = 7.20 > 7 MAX_NOTIONAL (and 0.03 leaves 0.01 < MIN_NOTIONAL dust!)
        h.state.bid = h.state.ask = h.state.mark = D('180.00')
        expected_reason = 'EXIT_MAX_NOTIONAL'
    else:
        h.state.bid = h.state.ask = h.state.mark = D('143.00')
        expected_reason = 'EXIT_GLOBAL_BUDGET_EXHAUSTED'

    h.state.stop_after_polls = 2
    assert h.start_execute() == 0
    # ZERO extra POSTs, retains original position, stays on old deployment, reports explicit EXIT_BLOCKED!
    assert len(writes(h.requests, 'POST')) == before_posts
    snap = json.loads((h.root / 'cloud-health.json').read_text())
    assert snap['grid']['deployment_id'] == old_id
    assert snap['grid']['state'] == 'PAUSED'
    bexit = snap['grid']['breakout_exit']
    assert bexit['phase'] == 'EXIT_BLOCKED'
    assert bexit['reason'] == expected_reason
    assert bexit['entry_blocked'] is True
    assert D(str(bexit['inventory'])) == h.fake.quantity


# ---------------------------------------------------------------------------
# 16. C04 systemd Execute path & honest cloud health across exit -> rotation
# ---------------------------------------------------------------------------

def test_f11_16_c04_execute_path_and_health_across_exit_to_rotation(f11_harness, capsys, monkeypatch):
    h = f11_harness
    h.state.use_dynamic_factory = True
    _, old_id, _ = _setup_breakout_with_position(h, breakout_price='143')
    h.state.bid, h.state.ask, h.state.mark = D('143.00'), D('143.10'), D('143.05')
    h.state.bar_close, h.state.bar_high, h.state.bar_low = D('143.05'), D('143.40'), D('142.80')
    health_trace = []

    def track(supervisor, raw, state):
        worker = supervisor.feed
        capsys.readouterr()
        code = cloud.health(h.root)
        payload = json.loads(capsys.readouterr().out)
        health_trace.append((
            code,
            payload['alive'],
            payload['account_recovered'],
            payload['market_fresh'],
            payload['execute_healthy'],
            payload['grid']['deployment_id'],
            payload['grid']['state'],
            payload['grid']['breakout_exit'],
        ))
        if state.polls == 1:
            # While exiting: alive + recovered + fresh, but execute_healthy=False (not normal RUNNING).
            assert code == 1
            assert payload['alive'] and payload['account_recovered'] and payload['market_fresh']
            assert not payload['execute_healthy']
            assert payload['grid']['breakout_exit']['phase'] == 'EXIT_WORKING'
            exit_id = payload['grid']['breakout_exit']['active_exit_order_id']
            h.fake.trade_update(exit_id, '0.04', '143.00')
            state.seconds += 1.0
        elif state.polls == 2:
            # Post-flat rotation: back to RUNNING on next generation, execute_healthy=True (exit code 0)!
            assert code == 0
            assert payload['execute_healthy'] is True
            assert payload['demo_execution_ready'] is True
            assert payload['grid']['deployment_id'] == old_id + '-00001'
            assert payload['grid']['state'] == 'RUNNING'
            assert payload['grid']['breakout_exit'] is None

    h.state.on_report = track
    h.state.stop_after_polls = 2
    assert h.start_execute() == 0
    assert len(health_trace) == 2
