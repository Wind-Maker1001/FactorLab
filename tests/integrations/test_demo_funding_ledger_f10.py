"""Offline authoritative funding ledger, recovery coverage and F09 reconciliation tests."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_binance_demo_execution import DemoFixture, intent, setup as demo_setup
from test_binance_demo_grid_network import network_grid, observe, writes
from test_binance_demo_network import NetworkFixture, wired

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.adapter import (
    INCOME_MAX_WINDOW_MS, INCOME_OVERLAP_MS,
)
from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import (
    capture, compare_recovery, experiment_evidence, fifo_evidence,
)
from factorlab.integrations.binance_futures_trading.demo_runtime import (
    DemoRuntime, open_grid_session,
)
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, Environment, SafetyBlocked,
)
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import FundingEvent, Market, OrderStatus as S, Side
from factorlab.trading.storage import PaperStore, decode, dumps


def _set_mark_price(monkeypatch, mark_price):
    original = NetworkFixture.__call__

    def marked(self, request, timeout):
        response = original(self, request, timeout)
        if request.path not in {'/fapi/v3/account', '/fapi/v3/positionRisk'}:
            return response
        payload = json.loads(response.body)
        mark = D(str(mark_price))
        pnl = self.quantity * (mark - self.entry)
        if request.path == '/fapi/v3/account':
            payload['assets'][0].update(
                unrealizedProfit=str(pnl),
                marginBalance=str(self.cash + pnl),
            )
            for position in payload['positions']:
                position['unrealizedProfit'] = str(pnl)
        else:
            for position in payload:
                position.update(markPrice=str(mark), notional=str(self.quantity * mark))
        return Response(response.status, response.headers, json.dumps(payload).encode())

    monkeypatch.setattr(NetworkFixture, '__call__', marked)


def test_single_negative_funding_booking(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.04', '146')
        bridge.recover()
        order_events_before = list(bridge.controller.store.order_events())
        fees_before = bridge.gateway.adapter.order(buy_id).cumulative_fee

        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
        bridge.recover()

        funding_events = [event for _, event in bridge.controller.store.funding_events()]
        assert len(funding_events) == 1
        event = funding_events[0]
        assert isinstance(event, FundingEvent)
        assert event.tran_id == '1768092951478927810'
        assert event.symbol == 'NVDAUSDT'
        assert event.asset == 'USDT'
        assert event.income_type == 'FUNDING_FEE'
        assert event.amount == D('-0.00469722')
        assert event.event_time == NOW
        assert event.account_id == bridge.session.adapter.account_id
        assert event.source == 'binance-rest-income'
        assert event.event_id == 'funding:FUNDING_FEE:1768092951478927810'
        assert event.metadata['endpoint'] == '/fapi/v1/income'
        # Never disguised as an order event, order fee or synthetic fill.
        assert list(bridge.controller.store.order_events()) == order_events_before
        assert bridge.gateway.adapter.order(buy_id).cumulative_fee == fees_before
        assert bridge.gateway.state.funding['USDT'] == D('-0.00469722')
        assert bridge.controller.account().assets[0].total == fake.cash == D('1000') - fees_before - D('0.00469722')

        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']
        assert report['statuses']['DEMO_FEE_RECONCILED']
        assert report['statuses']['DEMO_FUNDING_RECONCILED']
        assert report['statuses']['DEMO_POSITION_RECONCILED']
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert summary['realized_funding'] == D('-0.00469722')
        assert summary['actual_fees'] == fees_before
        assert summary['reconciliation_residual'] == 0
        assert report['funding']['booking'] == 'AUTHORITATIVE_LEDGER_RECONCILED'
        assert not report['unaccounted_income']


def test_positive_funding_income_booking(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.04', '146')
        bridge.recover()
        fees = bridge.gateway.adapter.order(buy_id).cumulative_fee

        fake.funding_update('0.01234567', tran_id=900001, time_ms=MS)
        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']
        expected_wallet = D('1000') - fees + D('0.01234567')
        assert report['wallet'] == expected_wallet
        assert report['equity'] == expected_wallet
        assert summary['realized_funding'] == D('0.01234567')
        assert summary['wallet_change'] == expected_wallet - D('1000')
        assert summary['final_nav'] == expected_wallet / D('1000')
        assert summary['reconciliation_residual'] == 0
        assert report['statuses']['DEMO_FUNDING_RECONCILED']
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED']


def test_repeated_get_same_tran_id_idempotent(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
        bridge.recover()
        first_account = bridge.controller.account()
        first_funding = list(bridge.controller.store.funding_events())
        first_state = bridge.controller.store.load()

        # Repeat GET with trailing-zero string equivalent for the same tranId.
        fake.income[0]['income'] = '-0.0046972200'
        for _ in range(3):
            bridge.recover()
            assert bridge.controller.account() == first_account
            assert list(bridge.controller.store.funding_events()) == first_funding
            assert bridge.controller.store.load() == first_state
            assert bridge.session.adapter.db.execute('SELECT count(*) FROM futures_funding').fetchone()[0] == 1


def test_restart_and_cross_process_no_duplicate_funding(network_grid, tmp_path, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
        before = capture(bridge.session, bridge.runtime, D('1000'))
        assert before['statuses']['DEMO_ACCOUNT_RECONCILED']
        expected_funding = list(bridge.controller.store.funding_events())

    # Reopen in a fresh session and verify no duplicate booking.
    with open_grid_session(root, store, factory) as bridge:
        after = capture(bridge.session, bridge.runtime, D('1000'))
        assert list(bridge.controller.store.funding_events()) == expected_funding
        facts = compare_recovery(before, after)
        assert all(facts.values())
        assert facts['funding_events_unchanged']
        assert after['restart_outcome'] == 'UNCHANGED'
        assert after['statuses']['DEMO_RESTART_RECOVERY_VERIFIED']

    # Verify independent child process GET-only reopening with persisted funding.
    protocol = tmp_path / 'proc_protocol.sqlite'
    ledger = tmp_path / 'proc_controller.sqlite'
    adapter, gateway, controller, fixture = demo_setup(protocol, ledger)
    controller.submit(intent())
    fixture.trade_update('one', '1', '100')
    fixture.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
    gateway.recover(controller)
    expected_json = dumps(list(controller.store.funding_events()))
    adapter.store.close()
    controller.store.close()

    payload = dict(
        orders=fixture.orders,
        trades=fixture.trades,
        income=fixture.income,
        cash=str(fixture.cash),
        quantity=str(fixture.quantity),
        entry=str(fixture.entry),
        funding_events=expected_json,
    )
    program = '''
import json, sys
from decimal import Decimal as D
sys.path[:0] = sys.argv[3:5]
from test_binance_demo_execution import setup, DemoFixture
from factorlab.trading.storage import dumps
fake = DemoFixture()
payload = json.load(sys.stdin)
fake.orders, fake.trades, fake.income = payload['orders'], payload['trades'], payload['income']
for field in ('cash', 'quantity', 'entry'):
    setattr(fake, field, D(payload[field]))
a, g, c, _ = setup(sys.argv[1], sys.argv[2], fake=fake, authorize=False)
for _ in range(2):
    account = g.recover(c)
    assert account.reconciled
    assert account.assets[0].total == fake.cash
    assert g.state.funding['USDT'] == D('-0.00469722')
    assert dumps(list(c.store.funding_events())) == payload['funding_events']
assert all(method == 'GET' for method, _, _ in fake.calls)
a.store.close()
c.store.close()
print('CROSS_PROCESS_FUNDING_VERIFIED')
'''
    result = subprocess.run(
        [sys.executable, '-c', program, str(protocol), str(ledger),
         str(Path(__file__).parent.resolve()),
         str(Path(__file__).resolve().parents[2] / 'src')],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'CROSS_PROCESS_FUNDING_VERIFIED'


def test_empty_sync_persists_watermark_across_demo_session_restart(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    current_time = NOW
    monkeypatch.setattr(DemoSession, '_now', lambda _: current_time)
    enable()
    with open_grid_session(root, store, factory) as bridge:
        adapter = bridge.session.adapter
        initial = adapter.sync_funding(start_time=MS - 16 * 86_400_000)
        assert initial.complete and initial.windows == 3
        current_time += timedelta(minutes=10)
        through_ms = int(current_time.timestamp() * 1000)
        synced = adapter.sync_funding()
        assert synced.newly_recorded == ()
        assert synced.covered_through_ms == through_ms
        assert adapter.db.execute(
            'SELECT covered_through_ms FROM futures_income_coverage'
        ).fetchone() == (through_ms,)

    with open_grid_session(root, store, factory) as bridge:
        adapter = bridge.session.adapter
        assert adapter.funding_coverage()['covered_through_ms'] == through_ms
        before_calls = len(fake.calls)
        synced = adapter.sync_funding()
        income_calls = [params for _, path, params in fake.calls[before_calls:]
                        if path == '/fapi/v1/income']
        assert synced.newly_recorded == ()
        assert synced.windows == 1
        assert len(income_calls) == 1
        assert int(income_calls[0]['startTime'][0]) == through_ms - INCOME_OVERLAP_MS
        assert int(income_calls[0]['endTime'][0]) == through_ms
    assert all(request.get_method() == 'GET' for request in requests)


def test_idle_funding_watermark_advances_across_process_restarts(tmp_path):
    protocol, ledger = tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite'
    fake = DemoFixture()
    fake.funding_update('-0.01', tran_id=701, time_ms=MS)
    adapter, gateway, controller, _ = demo_setup(protocol, ledger, fake=fake, authorize=False)
    try:
        expected = dumps(dict(
            cash=gateway.state.cash, positions=gateway.state.positions,
            funding=gateway.state.funding, events=list(controller.store.funding_events()),
            order_events=list(controller.store.order_events()),
        ))
        origin_ms = adapter.funding_coverage()['covered_from_ms']
    finally:
        adapter.store.close()
        controller.store.close()

    program = '''
import json, sqlite3, sys
from datetime import timedelta
from decimal import Decimal as D
sys.path[:0] = sys.argv[3:5]
from test_binance_demo_execution import setup, DemoFixture
from factorlab.integrations.binance_futures_trading import adapter as module
from factorlab.integrations.binance_futures_trading.fake import MS, NOW
from factorlab.trading.storage import dumps
payload = json.load(sys.stdin)
# Six-day increments fit one real seven-day window. A stale watermark would
# exceed this reduced budget by the third restart, without a years-long fixture.
module.INCOME_MAX_WINDOWS = 2
step_ms = 6 * 86_400_000
previous_ms = MS + (payload['cycle'] - 1) * step_ms
with sqlite3.connect(sys.argv[1]) as db:
    assert db.execute('SELECT covered_through_ms FROM futures_income_coverage').fetchone() == (previous_ms,)
fake = DemoFixture()
fake.funding_update('-0.01', tran_id=701, time_ms=MS)
now = NOW + timedelta(days=6 * payload['cycle'])
a, g, c, _ = setup(sys.argv[1], sys.argv[2], fake=fake, authorize=False, now=now)
try:
    coverage = a.funding_coverage()
    assert coverage['covered_from_ms'] == payload['origin_ms']
    assert coverage['covered_through_ms'] == previous_ms + step_ms
    assert a.db.execute('SELECT covered_through_ms FROM futures_income_coverage').fetchone() == (previous_ms + step_ms,)
    calls = [params for _, path, params in fake.calls if path == '/fapi/v1/income']
    assert len(calls) == 1
    assert int(calls[0]['startTime'][0]) == previous_ms - module.INCOME_OVERLAP_MS
    assert int(calls[0]['endTime'][0]) == previous_ms + step_ms
    assert coverage['detail']['windows'] == 1
    assert coverage['detail']['newly_recorded'] == 0
    assert a.db.execute('SELECT count(*) FROM futures_funding').fetchone() == (1,)
    assert dumps(dict(cash=g.state.cash, positions=g.state.positions,
        funding=g.state.funding, events=list(c.store.funding_events()),
        order_events=list(c.store.order_events()))) == payload['expected']
    assert c.account().assets[0].total == fake.cash == D('999.99')
    account = g.read_account()
    assert account.reconciled
    assert account.assets[0].total / D('1000') == D('0.99999')
    assert all(method == 'GET' for method, _, _ in fake.calls)
finally:
    a.store.close()
    c.store.close()
print('IDLE_WATERMARK_VERIFIED')
'''
    for cycle in range(1, 6):
        result = subprocess.run(
            [sys.executable, '-c', program, str(protocol), str(ledger),
             str(Path(__file__).parent.resolve()),
             str(Path(__file__).resolve().parents[2] / 'src')],
            input=json.dumps(dict(cycle=cycle, expected=expected, origin_ms=origin_ms)),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'IDLE_WATERMARK_VERIFIED'


@pytest.mark.parametrize('failure', ['network', 'pagination', 'malformed'])
def test_failed_funding_sync_keeps_watermark_and_retries_interval(tmp_path, failure):
    class FailingIncome(DemoFixture):
        failure = None

        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/income' and self.failure == 'network':
                raise TimeoutError('income GET failed')
            response = super().__call__(request, timeout)
            if request.path == '/fapi/v1/income' and self.failure == 'malformed':
                rows = json.loads(response.body)
                rows[-1]['income'] = 'NaN'
                return Response(200, {}, json.dumps(rows).encode())
            return response

    fake = FailingIncome()
    fake.funding_update('-0.01', tran_id=801, time_ms=MS)
    adapter, gateway, controller, _ = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite', fake=fake, authorize=False,
    )
    try:
        before = adapter.db.execute('SELECT * FROM futures_income_coverage').fetchall()
        events = adapter.funding_events()
        for number in range(1, 4):
            fake.funding_update('-0.01', tran_id=801 + number, time_ms=MS + number * 60_000)
        adapter.clock = lambda: NOW + timedelta(minutes=10)
        fake.failure = failure
        with pytest.raises(DataQualityError, match={
            'network': 'TRANSPORT_TIMEOUT', 'pagination': 'max pages', 'malformed': 'nonfinite',
        }[failure]):
            adapter.sync_funding(page_limit=2, max_pages=1)
        assert adapter.db.execute('SELECT * FROM futures_income_coverage').fetchall() == before
        assert adapter.funding_events() == events
        assert not adapter.reconciled

        fake.failure = None
        before_calls = len(fake.calls)
        synced = adapter.sync_funding()
        income_calls = [params for _, path, params in fake.calls[before_calls:]
                        if path == '/fapi/v1/income']
        assert int(income_calls[0]['startTime'][0]) == MS - INCOME_OVERLAP_MS
        assert [event.tran_id for event in synced.newly_recorded] == ['802', '803', '804']
        assert adapter.db.execute(
            'SELECT covered_through_ms FROM futures_income_coverage'
        ).fetchone() == (MS + 600_000,)
        assert gateway.recover(controller).reconciled
        assert gateway.state.funding['USDT'] == D('-0.04')
        assert all(method == 'GET' for method, _, _ in fake.calls)
    finally:
        adapter.store.close()
        controller.store.close()


def test_same_funding_watermark_skips_database_write(tmp_path):
    adapter, _, controller, fake = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite', authorize=False,
    )
    try:
        before = adapter.db.execute('SELECT * FROM futures_income_coverage').fetchall()
        changes = adapter.db.total_changes
        for end_ms in (MS, MS, MS - 60_000):
            synced = adapter.sync_funding(end_time=end_ms)
            assert synced.newly_recorded == ()
            assert synced.covered_through_ms == MS
            assert adapter.db.total_changes == changes
            assert adapter.db.execute('SELECT * FROM futures_income_coverage').fetchall() == before
        assert all(method == 'GET' for method, _, _ in fake.calls)
    finally:
        adapter.store.close()
        controller.store.close()


def test_late_arriving_funding_backfill_and_restart_delta(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    current_time = NOW
    monkeypatch.setattr(DemoSession, '_now', lambda _: current_time)
    enable()

    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.04', '146')
        fake.trades[-1]['realizedPnl'] = '0'
        before = capture(bridge.session, bridge.runtime, D('1000'))
        assert before['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert before['funding']['events'] == []

    # 5 minutes later, exchange materializes a funding settlement whose event time
    # was 10 seconds BEFORE the previous covered_through_ms (late-arriving settlement).
    late_ms = MS - 10_000
    fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=late_ms)
    current_time = NOW + timedelta(minutes=5)

    with open_grid_session(root, store, factory) as bridge:
        after = capture(bridge.session, bridge.runtime, D('1000'))
        assert after['statuses']['DEMO_FUNDING_RECONCILED']
        assert after['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert after['fifo']['attribution']['summary']['realized_funding'] == D('-0.00469722')
        assert after['fifo']['attribution']['summary']['reconciliation_residual'] == 0
        facts = compare_recovery(before, after)
        assert not facts['funding_events_unchanged']
        assert not facts['wallet_unchanged']
        assert facts['order_facts_unchanged']
        assert facts['fees_unchanged']
        assert after['restart_outcome'] == 'NATURAL_FILL_DELTA_VERIFIED'
        assert after['statuses']['DEMO_RESTART_RECOVERY_VERIFIED']
        assert after['restart_fill_delta']['funding_change'] == D('-0.00469722')
        assert after['restart_fill_delta']['wallet_change'] == D('-0.00469722')
        assert after['restart_fill_delta']['actual_fee_change'] == 0


@pytest.mark.parametrize(('field', 'mutated'), [
    ('income', '-0.00500000'),
    ('asset', 'BNB'),
    ('time', MS - 1000),
    ('symbol', 'BTCUSDT'),
])
def test_funding_identity_conflict_rejected_without_overwrite(tmp_path, field, mutated):
    adapter, gateway, controller, fake = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite',
    )
    try:
        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
        assert gateway.recover(controller).reconciled
        original_events = adapter.funding_events()
        original_controller_events = list(controller.store.funding_events())
        original_state = controller.store.load()

        # Mutate exchange payload for the SAME tranId.
        fake.income[0][field] = mutated
        with pytest.raises(DataQualityError):
            gateway.recover(controller)

        assert not adapter.reconciled
        assert not gateway.read_account().reconciled
        # Original confirmed funding must NEVER be silently overwritten.
        assert adapter.funding_events() == original_events
        assert list(controller.store.funding_events()) == original_controller_events
        assert controller.store.load().funding == original_state.funding
        assert controller.store.load().funding_events == original_state.funding_events
    finally:
        adapter.store.close()
        controller.store.close()


@pytest.mark.parametrize('bad_payload', [
    {'bad': 'not-a-list'},
    [{'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.01', 'asset': 'BNB', 'time': MS, 'tranId': 5}],
    [{'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': 'NaN', 'asset': 'USDT', 'time': MS, 'tranId': 6}],
    [{'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.01', 'asset': 'USDT', 'time': True, 'tranId': 7}],
    [{'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.01', 'asset': 'USDT', 'time': MS, 'tranId': ''}],
    [{'symbol': '', 'incomeType': 'FUNDING_FEE', 'income': '-0.01', 'asset': 'USDT', 'time': MS, 'tranId': 8}],
])
def test_malformed_or_unsupported_asset_funding_rejected(tmp_path, bad_payload):
    class BadIncomeFixture(DemoFixture):
        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/income' and self.inject_bad:
                return Response(200, {}, json.dumps(bad_payload).encode())
            return super().__call__(request, timeout)

    fake = BadIncomeFixture()
    fake.inject_bad = False
    adapter, gateway, controller, _ = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite', fake=fake,
    )
    try:
        fake.inject_bad = True
        with pytest.raises(DataQualityError):
            gateway.recover(controller)
        assert not adapter.reconciled
        assert not adapter.funding_events()
        assert not list(controller.store.funding_events())
    finally:
        adapter.store.close()
        controller.store.close()


def test_historical_backfill_bridges_to_incremental_sync(tmp_path):
    now = NOW
    fake = DemoFixture()
    adapter, gateway, controller, _ = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite', fake=fake, now=now,
    )
    try:
        # Simulate pre-F10 state where coverage was never initialized and historical
        # funding spans 16 days (> 2x 7-day Binance windows).
        with adapter.store.transaction():
            adapter.db.execute('DELETE FROM futures_income_coverage')
        t_hist_1 = MS - 15 * 86_400_000
        t_hist_2 = MS - 8 * 86_400_000
        fake.funding_update('-0.01', tran_id=101, time_ms=t_hist_1)
        fake.funding_update('-0.02', tran_id=102, time_ms=t_hist_2)

        start_origin = NOW - timedelta(days=16)
        backfill = adapter.sync_funding(start_time=start_origin, end_time=NOW)
        assert backfill.windows == 3  # 16 days split into 3 explicit <= 7-day chunks
        assert [e.tran_id for e in backfill.newly_recorded] == ['101', '102']
        assert backfill.covered_from_ms == int(start_origin.timestamp() * 1000)
        assert backfill.covered_through_ms == MS

        # Replay historical funding into controller.
        assert gateway.recover(controller).reconciled
        assert gateway.state.funding['USDT'] == D('-0.03')

        # Advance clock by 2 hours and settle a new funding event #3.
        t_new = MS + 3_600_000
        fake.funding_update('0.005', tran_id=103, time_ms=t_new)
        later = NOW + timedelta(hours=2)
        adapter.clock = lambda: later
        controller.clock = lambda: later
        gateway.clock = lambda: later

        before_calls = len(fake.calls)
        incremental = adapter.sync_funding()
        income_calls = [params for _, path, params in fake.calls[before_calls:] if path == '/fapi/v1/income']
        # Incremental sync only queries a single overlap window, never rescanning 16 days!
        assert incremental.windows == 1
        assert len(income_calls) == 1
        assert int(income_calls[0]['startTime'][0]) == MS - 3_600_000
        assert int(income_calls[0]['endTime'][0]) == int(later.timestamp() * 1000)
        assert [e.tran_id for e in incremental.newly_recorded] == ['103']
        assert incremental.covered_from_ms == int(start_origin.timestamp() * 1000)
        assert incremental.covered_through_ms == int(later.timestamp() * 1000)

        assert gateway.recover(controller).reconciled
        assert gateway.state.funding['USDT'] == D('-0.025')
        assert [e.tran_id for _, e in controller.store.funding_events()] == ['101', '102', '103']
    finally:
        adapter.store.close()
        controller.store.close()


def test_income_incomplete_pagination_and_insufficient_coverage(network_grid, tmp_path):
    class HookableFixture(DemoFixture):
        income_hook = None

        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/income' and self.income_hook is not None:
                result = self.income_hook(request, timeout)
                if result is not None:
                    return result
            return super().__call__(request, timeout)

    # 1. Single unwindowed income() returning < 1000 rows does NOT establish coverage.
    fake = HookableFixture()
    adapter, gateway, controller, _ = demo_setup(
        tmp_path / 'p1.sqlite', tmp_path / 'c1.sqlite', fake=fake,
    )
    try:
        with adapter.store.transaction():
            adapter.db.execute('DELETE FROM futures_income_coverage')
        assert adapter.income() == ()
        assert adapter.funding_coverage() is None
        assert not gateway.read_account().reconciled

        # 2. Clean multi-page pagination with boundary timestamp tie deduplication.
        fake.funding_update('-0.01', tran_id=1, time_ms=MS - 3000)
        fake.funding_update('-0.02', tran_id=2, time_ms=MS - 2000)
        fake.funding_update('-0.03', tran_id=3, time_ms=MS - 1000)
        fake.funding_update('-0.04', tran_id=4, time_ms=MS - 1000)
        fake.funding_update('-0.05', tran_id=5, time_ms=MS)
        synced = adapter.sync_funding(start_time=MS - 5000, end_time=MS, page_limit=3)
        assert synced.pages == 3
        assert [e.tran_id for e in synced.events] == ['1', '2', '3', '4', '5']

        # 3. Saturated timestamp boundary continues with numbered pages.
        with adapter.store.transaction():
            adapter.db.execute('DELETE FROM futures_income_coverage')
        fake.income = [
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.01', asset='USDT', time=MS, tranId=10),
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.02', asset='USDT', time=MS, tranId=11),
        ]
        synced = adapter.sync_funding(start_time=MS, end_time=MS, page_limit=2)
        assert [event.tran_id for event in synced.newly_recorded] == ['10', '11']
        with adapter.store.transaction():
            adapter.db.execute('DELETE FROM futures_income_coverage')

        # 4. Max pages exceeded -> explicit pagination failure.
        fake.income = [
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.01', asset='USDT', time=MS - 3000, tranId=20),
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.01', asset='USDT', time=MS - 2000, tranId=21),
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.01', asset='USDT', time=MS - 1000, tranId=22),
        ]
        with pytest.raises(DataQualityError, match='max pages'):
            adapter.sync_funding(start_time=MS - 5000, end_time=MS, page_limit=2, max_pages=1)
        assert adapter.funding_coverage() is None

        # 5. Missing boundary rows on page 2 -> explicit pagination failure.
        page_calls = [0]

        def drop_boundary(request, timeout):
            page_calls[0] += 1
            if page_calls[0] == 2:
                return Response(200, {}, b'[]')
            return None

        fake.income_hook = drop_boundary
        with pytest.raises(DataQualityError, match='boundary rows missing'):
            adapter.income_window(start_time=MS - 5000, end_time=MS, income_type='FUNDING_FEE', limit=2)

        # 6. Out-of-window timestamp returned by exchange -> explicit failure.
        fake.income_hook = lambda req, timeout: Response(200, {}, json.dumps([
            dict(symbol='NVDAUSDT', incomeType='FUNDING_FEE', income='-0.01',
                 asset='USDT', time=MS + 5000, tranId=99)
        ]).encode())
        with pytest.raises(DataQualityError, match='outside requested window'):
            adapter.income_window(start_time=MS - 5000, end_time=MS, income_type='FUNDING_FEE')
        fake.income_hook = None
    finally:
        adapter.store.close()
        controller.store.close()

    # 7. Acceptance detects insufficient historical coverage when history_start precedes covered_from_ms.
    root, store, factory, enable, net_fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        net_fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        bridge.recover()
        covered_from = bridge.session.adapter.funding_coverage()['covered_from_ms']
        short_report = capture(
            bridge.session, bridge.runtime, D('1000'),
            history_start=covered_from - 86_400_000,
        )
        assert not short_report['statuses']['DEMO_FUNDING_RECONCILED']
        assert not short_report['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert short_report['funding']['booking'] == 'COVERAGE_INCOMPLETE'


def test_transient_network_failure_preserves_state_and_retries(tmp_path):
    class FlakyIncome(DemoFixture):
        fail_income = False

        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/income' and self.fail_income:
                raise TimeoutError('transient income timeout')
            return super().__call__(request, timeout)

    fake = FlakyIncome()
    adapter, gateway, controller, _ = demo_setup(
        tmp_path / 'protocol.sqlite', tmp_path / 'controller.sqlite', fake=fake,
    )
    try:
        fake.funding_update('-0.01', tran_id=301, time_ms=MS)
        assert gateway.recover(controller).reconciled
        before_state = controller.store.load()
        before_events = list(controller.store.funding_events())

        fake.funding_update('-0.02', tran_id=302, time_ms=MS)
        fake.fail_income = True
        with pytest.raises(DataQualityError, match='TRANSPORT_TIMEOUT'):
            gateway.recover(controller)
        assert not adapter.reconciled
        assert controller.store.load() == before_state
        assert list(controller.store.funding_events()) == before_events

        # Retry after transient outage resolves cleanly with zero writes.
        fake.fail_income = False
        recovered = gateway.recover(controller)
        assert recovered.reconciled
        assert gateway.state.funding['USDT'] == D('-0.03')
        assert [e.tran_id for _, e in controller.store.funding_events()] == ['301', '302']
        assert not [call for call in fake.calls if call[0] != 'GET']
    finally:
        adapter.store.close()
        controller.store.close()


def test_funding_does_not_pollute_fees_or_fifo_cost_basis(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    _set_mark_price(monkeypatch, '150')
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.02', '146')
        fake.trade_update(buy_id, '.02', '146')
        bridge.recover()
        bridge.dispatch()
        sell_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(sell_id, '.02', '154')
        bridge.recover()

        baseline_fifo = fifo_evidence(bridge.session, D('1000'))
        baseline_summary = baseline_fifo['attribution']['summary']
        baseline_turnover = bridge.account().turnover

        # Settle both negative and positive funding while holding remaining 0.02 long.
        fake.funding_update('-0.015', tran_id=401, time_ms=MS)
        fake.funding_update('0.005', tran_id=402, time_ms=MS)
        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']

        # Trade FIFO, lots, matches, fees, entry price and turnover remain IDENTICAL.
        assert report['fifo']['attribution']['lots'] == baseline_fifo['attribution']['lots']
        assert report['fifo']['attribution']['matches'] == baseline_fifo['attribution']['matches']
        assert summary['normal_sell'] == baseline_summary['normal_sell']
        assert summary['residual'] == baseline_summary['residual']
        assert summary['actual_fees'] == baseline_summary['actual_fees'] == D('0.00892')
        assert summary['realized_trade_pnl'] == baseline_summary['realized_trade_pnl'] == D('0.16')
        assert summary['realized_trade_net'] == baseline_summary['realized_trade_net']
        assert summary['trading_net'] == baseline_summary['trading_net']
        assert bridge.account().turnover == baseline_turnover
        assert bridge.gateway.state.positions['NVDAUSDT'].entry_price == D('146')
        # Funding is cleanly separated and included in strategy net P/L.
        assert summary['realized_funding'] == D('-0.010')
        assert summary['strategy_net_pnl'] == summary['trading_net'] + D('-0.010')
        assert summary['economic_net'] == summary['strategy_net_pnl']
        assert summary['reconciliation_residual'] == 0
        assert report['statuses']['DEMO_FEE_RECONCILED']
        assert report['statuses']['DEMO_FUNDING_RECONCILED']
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED']


def test_wallet_equity_nav_and_risk_no_double_deduction(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    _set_mark_price(monkeypatch, '150')
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.04', '146')
        bridge.recover()

        # Settle -0.60 USDT funding (official walletBalance drops by 0.60 once).
        # Max loss is 1.00 USDT: if funding were double-deducted, loss would look like
        # ~1.04584 > 1.00 and falsely block risk admission!
        fake.funding_update('-0.60', tran_id=501, time_ms=MS)
        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']
        expected_wallet = D('1000') - D('0.00584') - D('0.60')
        expected_unrealized = D('0.04') * (D('150') - D('146'))  # +0.16
        expected_equity = expected_wallet + expected_unrealized  # 999.55416
        assert report['wallet'] == expected_wallet
        assert report['equity'] == expected_equity
        assert summary['final_wallet'] == expected_wallet
        assert summary['final_equity'] == expected_equity
        assert summary['final_nav'] == expected_equity / D('1000')
        assert summary['wallet_change'] == expected_wallet - D('1000')
        assert summary['reconciliation_residual'] == 0
        assert bridge.account().equity == expected_equity
        assert bridge.controller.account().assets[0].total == expected_wallet

        # Risk admission still permits reduce-only SELL (actual loss = 0.44584 < 1.00, no double deduction!).
        sell = bridge.dispatch()
        assert sell is not None and sell.status is S.SUBMITTED
        assert len(writes(requests)) == 2

        # Another -0.60 funding brings actual loss to 1.04584 >= 1.00 -> risk loss cap blocks further POST!
        fake.cancel_test = True
        fake.funding_update('-0.60', tran_id=502, time_ms=MS)
        bridge.recover()
        with pytest.raises(SafetyBlocked, match='loss'):
            bridge.runtime.reserve(
                bridge.runtime.active_campaign(),
                'POST',
                dict(symbol='NVDAUSDT', side='BUY', type='LIMIT', timeInForce='GTC',
                     positionSide='BOTH', quantity='0.04', price='146', reduceOnly='false',
                     newClientOrderId=bridge.runtime.active_campaign()['prefix'] + '-x'),
                bridge.session.client.transport.authorization.plan,
                expected=(bridge.runtime.active_campaign()['prefix'] + '-x', 'BUY', D('0.04'), 'false'),
                facts=bridge.session.adapter.facts,
            )


def test_zero_funding_account_preserves_behavior(network_grid):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']
        assert report['funding']['events'] == []
        assert report['funding']['booked_events'] == []
        assert report['funding']['booking'] == 'NO_EVENT_OBSERVED_IN_WINDOW'
        assert summary['realized_funding'] == 0
        assert summary['trading_net'] == summary['strategy_net_pnl'] == summary['economic_net']
        assert summary['reconciliation_residual'] == 0
        assert report['statuses']['DEMO_FEE_RECONCILED']
        assert report['statuses']['DEMO_FUNDING_RECONCILED']
        assert report['statuses']['DEMO_POSITION_RECONCILED']
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED']


@pytest.mark.parametrize('income_type,symbol,asset', [
    ('TRANSFER', '', 'USDT'),
    ('INSURANCE_CLEAR', 'NVDAUSDT', 'USDT'),
    ('COMMISSION_REBATE', 'NVDAUSDT', 'USDT'),
    ('INTERNAL_TRANSFER', '', 'USDT'),
])
def test_unsupported_income_types_block_account_reconciliation(network_grid, income_type, symbol, asset):
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        fake.trade_update(bridge.grid.active[0].intent.client_order_id, '.04', '146')
        # Valid booked funding alongside an unsupported income type.
        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
        unsupported = dict(symbol=symbol, incomeType=income_type, income='0.50',
                           asset=asset, time=MS, tranId=8888, tradeId='')
        fake.income.append(unsupported)
        report = capture(bridge.session, bridge.runtime, D('1000'))
        # Order fees and funding are reconciled, but unsupported income blocks account acceptance!
        assert report['statuses']['DEMO_FEE_RECONCILED']
        assert report['statuses']['DEMO_FUNDING_RECONCILED']
        assert not report['statuses']['DEMO_ACCOUNT_RECONCILED']
        assert unsupported in report['unaccounted_income']


def test_f09_deterministic_funding_residual_reconciliation(network_grid, monkeypatch):
    """Exact deterministic F09 regression: -0.00469722 USDT residual reconciled to 0."""
    root, store, _, enable, fake, requests = network_grid
    opening_wallet = D('4999.97114760')
    expected_wallet = D('4999.96645038')
    fake.cash = opening_wallet
    mark_price = D('129.59')
    base_call = NetworkFixture.__call__

    def f09_market(self, request, timeout):
        if request.path.endswith('bookTicker'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","bidPrice":"130.90","askPrice":"130.90","time":%d}' % MS)
        if request.path.endswith('premiumIndex'):
            return Response(200, {}, b'{"symbol":"NVDAUSDT","markPrice":"129.59","time":%d}' % MS)
        response = base_call(self, request, timeout)
        if request.method == 'POST' and request.path == '/fapi/v1/order':
            payload = json.loads(response.body)
            cid = payload['clientOrderId']
            self.orders[cid]['orderId'] = 194372514
            payload['orderId'] = 194372514
            return Response(response.status, response.headers, json.dumps(payload).encode())
        if request.path in {'/fapi/v3/account', '/fapi/v3/positionRisk'}:
            payload = json.loads(response.body)
            pnl = (self.quantity * (mark_price - self.entry)).quantize(D('0.00000001'))
            if request.path == '/fapi/v3/account':
                payload['assets'][0].update(
                    unrealizedProfit=str(pnl),
                    marginBalance=str(self.cash + pnl),
                )
                for position in payload['positions']:
                    position['unrealizedProfit'] = str(pnl)
            else:
                for position in payload:
                    position.update(markPrice=str(mark_price), notional=str(self.quantity * mark_price))
            return Response(response.status, response.headers, json.dumps(payload).encode())
        return response

    monkeypatch.setattr(NetworkFixture, '__call__', f09_market)
    current_time = NOW
    monkeypatch.setattr(DemoSession, '_now', lambda _: current_time)
    enable()

    def f09_factory(session, deployment_id):
        return GridExecution(
            GridConfig(lower=129.59, upper=132.21, levels=1, initial_price=130.90,
                       initial_inventory=0, quantity=1, max_inventory=1, max_cash_committed=7,
                       breakout='pause'),
            GridRules(D('.04'), D('.01'), D('.01'), D('.01'), D('5')),
            deployment_id=deployment_id, account_id=session.adapter.account_id,
            market=Market.BINANCE_USDM, instrument_id='NVDAUSDT', initial_equity=opening_wallet,
        )

    with open_grid_session(root, store, f09_factory) as bridge:
        bridge.recover()
        bridge.observe(dict(id='f09-quote', time='2026-11-11T12:00:00', price='130.90', volume=1))
        buy_id = bridge.grid.active[0].intent.client_order_id
        assert bridge.grid.active[0].intent.limit_price == D('129.59')
        assert bridge.grid.active[0].intent.quantity == D('0.04')

        # Exact F09 exchange order 194372514 filled in two 0-fee Maker trades at 129.59 USDT.
        fake.orders[buy_id]['orderId'] = 194372514
        fake.orders[buy_id].update(
            executedQty='0.04',
            cumQuote='5.1836',
            status='FILLED',
            updateTime=MS + 1,
        )
        fake.trades.extend([
            dict(symbol='NVDAUSDT', id=40100001, orderId=194372514, qty='0.02',
                 quoteQty='2.5918', commission='0', commissionAsset='USDT', realizedPnl='0', time=MS + 1),
            dict(symbol='NVDAUSDT', id=40100002, orderId=194372514, qty='0.02',
                 quoteQty='2.5918', commission='0', commissionAsset='USDT', realizedPnl='0', time=MS + 1),
        ])
        fake.quantity = D('0.04')
        fake.entry = D('129.59')
        current_time = NOW + timedelta(milliseconds=1)
        bridge.recover()
        assert bridge.controller.account().assets[0].total == opening_wallet
        assert bridge.gateway.adapter.order(buy_id).cumulative_fee == D('0')

        # Exact F09 funding settlement: -0.00469722 USDT, tranId=1768092951478927810.
        fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS + 2)
        current_time = NOW + timedelta(milliseconds=2)
        assert fake.cash == expected_wallet

        # Confirm that BEFORE booking funding, the unbooked settlement reproduces F09's exact residual.
        with monkeypatch.context() as patch:
            patch.setattr('factorlab.integrations.binance_futures_trading.demo_fill_acceptance.recover',
                          lambda session: session.status())
            bridge.gateway.state.cash['USDT'] = expected_wallet
            bridge.session.adapter.facts = replace(
                bridge.session.adapter.facts,
                observed_at=current_time,
                assets=(dict(bridge.session.adapter.facts.assets[0],
                             walletBalance=str(expected_wallet),
                             marginBalance=str(expected_wallet)),),
            )
            unbooked = capture(bridge.session, bridge.runtime, opening_wallet)
            assert unbooked['fifo']['attribution']['summary']['reconciliation_residual'] == D('-0.00469722')
            assert not unbooked['statuses']['DEMO_FUNDING_RECONCILED']
            assert not unbooked['statuses']['DEMO_ACCOUNT_RECONCILED']
            bridge.gateway.state.cash['USDT'] = opening_wallet

        # After F10 GET recovery, the -0.00469722 USDT residual is explained and equals exact 0!
        reconciled = capture(bridge.session, bridge.runtime, opening_wallet)
        summary = reconciled['fifo']['attribution']['summary']
        assert reconciled['wallet'] == expected_wallet
        assert reconciled['equity'] == expected_wallet
        assert reconciled['actual_fees'] == D('0')
        assert summary['actual_fees'] == D('0')
        assert summary['residual_inventory'] == D('0.04')
        assert summary['realized_trade_pnl'] == D('0')
        assert summary['realized_funding'] == D('-0.00469722')
        assert summary['wallet_change'] == D('-0.00469722')
        assert summary['strategy_net_pnl'] == D('-0.00469722')
        assert summary['final_nav'] == expected_wallet / opening_wallet
        assert summary['reconciliation_residual'] == D('0')
        assert summary['reported_equity_difference'] == D('0')
        assert reconciled['funding']['booking'] == 'AUTHORITATIVE_LEDGER_RECONCILED'
        assert reconciled['funding']['unbooked_events'] == []
        assert reconciled['unaccounted_income'] == []
        assert reconciled['statuses']['DEMO_FEE_RECONCILED'] is True
        assert reconciled['statuses']['DEMO_FUNDING_RECONCILED'] is True
        assert reconciled['statuses']['DEMO_POSITION_RECONCILED'] is True
        assert reconciled['statuses']['DEMO_ACCOUNT_RECONCILED'] is True
        assert [e['tran_id'] for e in reconciled['fifo']['funding_events']] == ['1768092951478927810']

    # Restart verification on the exact F09 ledger.
    with open_grid_session(root, store, f09_factory) as bridge:
        restarted = capture(bridge.session, bridge.runtime, opening_wallet)
        facts = compare_recovery(reconciled, restarted)
        assert all(facts.values())
        assert restarted['statuses']['DEMO_ACCOUNT_RECONCILED'] is True
        assert restarted['statuses']['DEMO_RESTART_RECOVERY_VERIFIED'] is True
        assert restarted['fifo']['attribution']['summary']['reconciliation_residual'] == D('0')

        # F13 observed this mark with 0.04 NVDA and exchange PnL rounded to eight places.
        mark_price = D('132.80974812')
        marked = capture(bridge.session, bridge.runtime, opening_wallet)
        assert marked['official_equity_difference'] == D('0.0000000048')
        assert marked['statuses']['DEMO_ACCOUNT_RECONCILED'] is True


def test_pre_f10_persisted_journals_upgrade_and_backfill_funding(network_grid):
    """Existing pre-F10 SQLite snapshots lacking funding fields upgrade cleanly and backfill."""
    root, store, factory, enable, fake, requests = network_grid
    enable()
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        observe(bridge)
        buy_id = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(buy_id, '.04', '146')
        bridge.recover()
        # Simulate pre-F10 SQLite schema on disk: drop new tables and strip new PaperState fields.
        with bridge.session.adapter.store.transaction():
            bridge.session.adapter.db.execute('DROP TABLE futures_funding')
            bridge.session.adapter.db.execute('DROP TABLE futures_income_coverage')
        with bridge.controller.store.transaction():
            raw = json.loads(bridge.controller.store.db.execute(
                'SELECT payload FROM snapshot WHERE id=1').fetchone()[0])
            raw['fields'].pop('funding', None)
            raw['fields'].pop('funding_events', None)
            bridge.controller.store.db.execute(
                'UPDATE snapshot SET payload=? WHERE id=1', (json.dumps(raw),))

    # Funding settled on exchange while offline before upgrading to F10.
    fake.funding_update('-0.00469722', tran_id=1768092951478927810, time_ms=MS)
    with open_grid_session(root, store, factory) as bridge:
        report = capture(bridge.session, bridge.runtime, D('1000'))
        summary = report['fifo']['attribution']['summary']
        assert report['statuses']['DEMO_FUNDING_RECONCILED'] is True
        assert report['statuses']['DEMO_ACCOUNT_RECONCILED'] is True
        assert summary['realized_funding'] == D('-0.00469722')
        assert summary['reconciliation_residual'] == D('0')
        assert [e['tran_id'] for e in report['fifo']['funding_events']] == ['1768092951478927810']
