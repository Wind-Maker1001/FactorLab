"""P7-M19-F12: Incremental Demo order recovery, archival, and fault regressions.

Deterministic offline fixtures and temporary SQLite stores only. NO LIVE MONEY.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import subprocess
import sys
from urllib.parse import parse_qs

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.adapter import ACCOUNT_READ_SECONDS, FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, MS, NOW, synthetic_rules
from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, ExchangeError, FuturesClient, OutcomeUnknown, SafetyBlocked,
)
from factorlab.integrations.binance_futures_trading.recovery import (
    EXCHANGE_HISTORY_RETENTION_SECONDS, OrderRecoveryConfig, UNFILLED_CANCEL_RETENTION_SECONDS,
)
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading import Market, OrderIntent, OrderSnapshot, OrderStatus as S, OrderType, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.state import OrderRecord, unknown
from factorlab.trading.storage import PaperStore, dumps


class MutableClock:
    def __init__(self, current=NOW):
        self.current = current

    def utc(self):
        return self.current

    def epoch(self):
        return self.current.timestamp()

    def advance(self, seconds):
        self.current = self.current + timedelta(seconds=seconds)


def make_intent(key, *, side=Side.BUY, reduce_only=False, limit='100', quantity='1'):
    return OrderIntent(
        Market.BINANCE_USDM, 'NVDAUSDT', side,
        OrderType.MARKET if limit is None else OrderType.LIMIT,
        D(quantity), None if limit is None else D(limit),
        key, position_side='BOTH', reduce_only=reduce_only,
    )


def make_adapter(path, fake, clock, *, recovery_config=None, continuous_capacity=True):
    client = FuturesClient(
        Credentials('fixture-key', 'fixture-secret'),
        transport=fake, clock=clock.epoch,
    )
    store = PaperStore(path, continuous_capacity=continuous_capacity, research_wal=True)
    return FuturesAdapter(client, store, account_id='offline', clock=clock.utc,
                          recovery_config=recovery_config)


def make_gateway_stack(adapter, paper_path, clock, *, cash=D('100000')):
    gateway = FuturesPaperGateway(
        adapter, synthetic_rules(),
        (Instrument('NVDAUSDT', 'USDT'),), {'USDT': cash},
    )
    controller = TradingController(
        gateway, PaperStore(paper_path, continuous_capacity=True, research_wal=True),
        clock=clock.utc, limits=RiskLimits(D('50000'), D('50000'), D('80000'), 1000),
    )
    controller.quote(Quote('NVDAUSDT', D('100'), clock.utc()))
    return gateway, controller


def seed_historical_order(adapter, fake, *, key, order_id, status='CANCELED',
                          filled_qty=D('0'), price=D('100'), side=Side.BUY,
                          update_time_ms=None, trade_id=None, reduce_only=False,
                          commit=True):
    qty = D('1')
    filled = D(filled_qty)
    quote = filled * price
    fee = quote * D('0.001') if filled > 0 else D('0')
    update_ms = (MS - 3600 * 1000) if update_time_ms is None else update_time_ms
    updated_dt = datetime.fromtimestamp(update_ms / 1000, timezone.utc)
    mapped_status = S.FILLED if status == 'FILLED' else (
        S.CANCELED if status in {'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH'} else (
            S.PARTIALLY_FILLED if status == 'PARTIALLY_FILLED' else S.SUBMITTED
        )
    )
    intent = make_intent(key, side=side, reduce_only=reduce_only, limit=str(price), quantity=str(qty))
    snapshot = replace(
        OrderSnapshot(intent), status=mapped_status,
        cumulative_filled_quantity=filled, cumulative_fee=fee,
        exchange_order_id=str(order_id),
    )
    if commit:
        with adapter.store.transaction():
            adapter._save(OrderRecord(snapshot, updated_dt, price, mapped_status), quote)
    else:
        adapter._save(OrderRecord(snapshot, updated_dt, price, mapped_status), quote)
    fake.orders[key] = dict(
        clientOrderId=key, orderId=order_id, symbol='NVDAUSDT',
        side=side.value, type='LIMIT', positionSide='BOTH',
        origQty=str(qty), executedQty=str(filled), cumQuote=str(quote),
        status=status, updateTime=update_ms,
    )
    if filled > 0:
        tid = (len(fake.trades) + 1) if trade_id is None else trade_id
        fake.trades.append(dict(
            symbol='NVDAUSDT', id=tid, orderId=order_id, qty=str(filled),
            quoteQty=str(quote), commission=str(fee), commissionAsset='USDT',
            time=update_ms,
        ))
        signed = filled if side is Side.BUY else -filled
        if fake.quantity * signed < 0:
            fake.cash += min(abs(fake.quantity), filled) * (price - fake.entry) * (1 if fake.quantity > 0 else -1)
        new_qty = fake.quantity + signed
        if fake.quantity * signed >= 0 and new_qty != 0:
            fake.entry = (abs(fake.quantity) * fake.entry + filled * price) / abs(new_qty)
        elif fake.quantity * new_qty <= 0:
            fake.entry = price if new_qty else D('0')
        fake.quantity = new_qty
        fake.cash -= fee


def archive_seeded_orders(adapter, clock):
    """Run two authoritative GET confirmations past settle window so terminal orders archive."""
    assert adapter.reconcile().reconciled
    clock.advance(1)
    assert adapter.reconcile().reconciled


# ---------------------------------------------------------------------------
# 1. Fault: POST ACK lost (never duplicate POST)
# ---------------------------------------------------------------------------

def test_f12_lost_post_ack_recovers_without_duplicate_post(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        adapter.reconcile()
        fake.timeout_next = True
        order_intent = make_intent('ack-lost-1')
        with pytest.raises(OutcomeUnknown):
            adapter.submit_order(order_intent)
        assert adapter.classify_orders()['unknown'] == ('ack-lost-1',)
        assert adapter.recovery_status()['has_unknown'] is True
        with pytest.raises(OutcomeUnknown, match='duplicate submission forbidden'):
            adapter.submit_order(order_intent)
        with pytest.raises(SafetyBlocked, match='RECONCILE_REQUIRED'):
            adapter.submit_order(make_intent('replacement-order'))
        snap = adapter.reconcile()
        assert snap.reconciled
        assert adapter.order('ack-lost-1').status is S.SUBMITTED
        assert adapter.recovery_status()['has_unknown'] is False
        assert sum(1 for method, _, _ in fake.calls if method == 'POST') == 1
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 2. Fault: GET /order transient -2013 & retention boundary
# ---------------------------------------------------------------------------

def test_f12_transient_order_not_found_keeps_unknown_closed_then_recovers(tmp_path):
    clock = MutableClock()

    class TransientMissing(FakeTransport):
        def __init__(self):
            super().__init__()
            self.hide_key = None

        def __call__(self, request, timeout):
            if request.method == 'GET' and request.path == '/fapi/v1/order':
                params = parse_qs(request.query)
                if params.get('origClientOrderId') == [self.hide_key]:
                    self.calls.append((request.method, request.path, params))
                    return Response(400, {}, b'{"code":-2013}')
            return super().__call__(request, timeout)

    fake = TransientMissing()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        adapter.reconcile()
        fake.timeout_next = True
        with pytest.raises(OutcomeUnknown):
            adapter.submit_order(make_intent('transient-1'))
        fake.hide_key = 'transient-1'
        with pytest.raises(ExchangeError) as caught:
            adapter.reconcile()
        assert caught.value.code == -2013
        assert not adapter.reconciled
        assert adapter.order('transient-1').reconcile_required
        assert adapter.classify_orders()['unknown'] == ('transient-1',)
        with pytest.raises(SafetyBlocked):
            adapter.submit_order(make_intent('replacement-after-2013'))

        # Exchange becomes consistent: recovers cleanly with zero duplicate POST.
        fake.hide_key = None
        assert adapter.reconcile().reconciled
        assert adapter.order('transient-1').status is S.SUBMITTED
        assert sum(1 for method, _, _ in fake.calls if method == 'POST') == 1
    finally:
        adapter.store.close()


def test_f12_archived_order_retention_pruned_vs_in_window_2013(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        old_ms = MS - (UNFILLED_CANCEL_RETENTION_SECONDS + 3600) * 1000
        recent_ms = MS - 600 * 1000
        seed_historical_order(adapter, fake, key='old-cancel', order_id=1, status='CANCELED', update_time_ms=old_ms)
        seed_historical_order(adapter, fake, key='recent-cancel', order_id=2, status='CANCELED', update_time_ms=recent_ms)
        archive_seeded_orders(adapter, clock)
        assert set(adapter.classify_orders()['archived_exchange']) == {'old-cancel', 'recent-cancel'}

        # Prune only the >3d unfilled canceled order from exchange (documented Binance 3-day policy).
        del fake.orders['old-cancel']
        summary = adapter.audit_history(force=True, audit_batch_size=5)
        assert 'old-cancel' in summary['pruned_by_exchange_retention']
        assert adapter.recovery_status()['history_audit_status'] == 'OK'
        assert adapter.order('old-cancel').status is S.CANCELED

        # Deleting a recent in-retention archived order MUST fail closed!
        del fake.orders['recent-cancel']
        with pytest.raises(ExchangeError) as caught:
            adapter.audit_history(force=True, audit_batch_size=5)
        assert caught.value.code == -2013
        assert not adapter.reconciled
        assert adapter.order('recent-cancel').reconcile_required
        assert adapter.recovery_status()['history_audit_status'] == 'CONFLICT'
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 3. Fault: GET /userTrades pagination & page/stall failure
# ---------------------------------------------------------------------------

def test_f12_user_trades_pagination_and_cap_fail_closed(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    for tid in range(1, 8):
        fake.trades.append(dict(
            symbol='NVDAUSDT', id=tid, orderId=tid, qty='1', quoteQty='100',
            commission='0.1', commissionAsset='USDT', time=MS + tid * 1000,
        ))
    config = OrderRecoveryConfig(trade_page_limit=2, trade_max_pages=10)
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock, recovery_config=config)
    try:
        rows = adapter.user_trades_paginated('NVDAUSDT', from_id=1, limit=2, max_pages=10)
        assert [row['id'] for row in rows] == [1, 2, 3, 4, 5, 6, 7]
        trade_gets = [c for c in fake.calls if c[1] == '/fapi/v1/userTrades']
        assert len(trade_gets) == 4  # [1,2], [3,4], [5,6], [7]

        # Exceeding max_pages must fail closed.
        with pytest.raises(DataQualityError, match='page cap'):
            adapter.user_trades_paginated('NVDAUSDT', from_id=1, limit=2, max_pages=2)

        # >7-day time range chunks explicitly across 7-day windows.
        start_ms = MS - 10 * 24 * 3600 * 1000
        end_ms = MS + 10_000
        chunked = adapter.user_trades_paginated('NVDAUSDT', start_time=start_ms, end_time=end_ms, limit=100, max_pages=10)
        assert [row['id'] for row in chunked] == [1, 2, 3, 4, 5, 6, 7]

        # Combining fromId with startTime violates Binance API rules and is rejected.
        with pytest.raises(ValueError, match='forbids combining fromId'):
            adapter.user_trades('NVDAUSDT', from_id=1, start_time=MS)
    finally:
        adapter.store.close()


def test_f12_user_trades_stalled_pagination_fails_closed(tmp_path):
    clock = MutableClock()

    class StalledTrades(FakeTransport):
        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/userTrades':
                self.calls.append((request.method, request.path, parse_qs(request.query)))
                return Response(200, {}, json.dumps([
                    dict(symbol='NVDAUSDT', id=1, orderId=1, qty='1', quoteQty='100',
                         commission='0.1', commissionAsset='USDT', time=MS),
                    dict(symbol='NVDAUSDT', id=2, orderId=1, qty='1', quoteQty='100',
                         commission='0.1', commissionAsset='USDT', time=MS),
                ]).encode())
            return super().__call__(request, timeout)

    adapter = make_adapter(tmp_path / 'protocol.sqlite', StalledTrades(), clock)
    try:
        with pytest.raises(DataQualityError, match='did not advance'):
            adapter.user_trades_paginated('NVDAUSDT', from_id=1, limit=2, max_pages=5)
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 4. Fault: Delayed fill on archived order (reactive + periodic discovery)
# ---------------------------------------------------------------------------

def test_f12_archived_canceled_order_late_fill_discovered_and_booked(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        for i in range(25):
            seed_historical_order(
                adapter, fake, key=f'hist-{i}', order_id=i + 1,
                status='FILLED' if i % 2 else 'CANCELED',
                filled_qty=D('1') if i % 2 else D('0'),
                side=Side.BUY if (i // 2) % 2 == 0 else Side.SELL,
            )
        archive_seeded_orders(adapter, clock)
        assert len(adapter.classify_orders()['archived_exchange']) == 25

        # Steady-state round: zero /fapi/v1/order and zero /fapi/v1/userTrades GETs!
        start_calls = len(fake.calls)
        assert adapter.reconcile().reconciled
        steady_counts = Counter(path for m, path, _ in fake.calls[start_calls:] if m == 'GET')
        assert steady_counts['/fapi/v1/order'] == 0
        assert steady_counts['/fapi/v1/userTrades'] == 0

        # Late fill arrives on archived CANCELED order hist-0!
        fake.trade_update('hist-0', '1', '100')
        start_calls = len(fake.calls)
        assert adapter.reconcile().reconciled
        late_counts = Counter(path for m, path, _ in fake.calls[start_calls:] if m == 'GET')
        assert adapter.order('hist-0').status is S.FILLED
        assert adapter.order('hist-0').cumulative_filled_quantity == D('1')
        assert adapter.order('hist-0').cumulative_fee == D('0.1')
        # Only hist-0 was queried, NOT all 25 orders!
        assert late_counts['/fapi/v1/order'] == 1
        assert adapter.last_reconcile_summary['reactive_audit_triggered'] is True
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 5. Fault: Late fee (incomplete trades & retroactive commission correction)
# ---------------------------------------------------------------------------

def test_f12_late_fee_incomplete_trades_and_retroactive_commission_correction(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    config = OrderRecoveryConfig(trade_overlap_ids=5, history_audit_batch_size=2)
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock, recovery_config=config)
    try:
        # Part A: Filled order whose userTrades lags behind cannot archive and stays UNKNOWN.
        seed_historical_order(adapter, fake, key='lag-fee', order_id=1, status='NEW')
        assert adapter.reconcile().reconciled
        fake.trade_update('lag-fee', '1', '100')
        saved_trade = dict(fake.trades[-1])
        fake.trades.pop()
        with pytest.raises(DataQualityError, match='fee UNKNOWN'):
            adapter.reconcile()
        assert adapter.classify_orders()['unknown'] == ('lag-fee',)
        assert not adapter.reconciled

        # Delayed trade + USDT fee arrives -> reconciles cleanly!
        fake.trades.append(saved_trade)
        assert adapter.reconcile().reconciled
        assert adapter.order('lag-fee').cumulative_fee == D('0.1')

        # Part B: Seed 15 archived filled orders (trade IDs exceed overlap_ids=5).
        for i in range(15):
            seed_historical_order(
                adapter, fake, key=f'old-filled-{i}', order_id=i + 10,
                status='FILLED', filled_qty=D('1'), price=D('100'),
                side=Side.BUY if i % 2 == 0 else Side.SELL,
            )
        clock.advance(200)
        archive_seeded_orders(adapter, clock)
        adapter.audit_history(force=True, audit_batch_size=0)
        assert adapter.reconcile().reconciled

        # Retroactively modify fee on the OLDEST trade (outside 5-ID overlap window!) and adjust wallet.
        oldest = next(t for t in fake.trades if t['orderId'] == 10)
        oldest['commission'] = '0.25'
        fake.cash -= D('0.15')
        with pytest.raises((DataQualityError, ValueError)):
            adapter.reconcile()
        assert not adapter.reconciled
        assert adapter.order('old-filled-0').reconcile_required
        assert adapter.recovery_status()['history_audit_status'] == 'CONFLICT'
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 6. Fault: Terminal order with incomplete economic facts cannot archive
# ---------------------------------------------------------------------------

def test_f12_terminal_order_with_incomplete_economics_cannot_archive(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    config = OrderRecoveryConfig(terminal_settle_seconds=60, cancel_settle_seconds=120, min_terminal_confirmations=2)
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock, recovery_config=config)
    try:
        adapter.reconcile()
        # A. WRITE_ACK-only CANCELED order (never GET-confirmed) -> ECONOMIC_INCOMPLETE.
        adapter.submit_order(make_intent('write-ack-cancel'))
        adapter.cancel_order('write-ack-cancel')
        assert adapter.classify_orders()['economic_incomplete'] == ('write-ack-cancel',)

        # B. WebSocket-only FILLED order (never GET-confirmed) -> ECONOMIC_INCOMPLETE.
        adapter.submit_order(make_intent('ws-fill'))
        ws_msg = fake.trade_update('ws-fill', '1', '100')
        UserDataEvents(adapter).parse(ws_msg)
        assert 'ws-fill' in adapter.classify_orders()['economic_incomplete']

        # C. PENDING_CANCEL raw status -> ACTIVE, never archived!
        seed_historical_order(adapter, fake, key='pending-cancel', order_id=99, status='NEW', update_time_ms=MS)
        fake.orders['pending-cancel']['status'] = 'PENDING_CANCEL'
        adapter.query_order('pending-cancel')
        assert 'pending-cancel' in adapter.classify_orders()['active']

        # First GET confirm of terminal orders (inside settle window): still ECONOMIC_INCOMPLETE!
        fake.orders['pending-cancel']['status'] = 'CANCELED'
        assert adapter.reconcile().reconciled
        assert set(adapter.classify_orders()['economic_incomplete']) == {'write-ack-cancel', 'ws-fill', 'pending-cancel'}

        # Advance 70s (>60s FILLED settle, <120s CANCELED settle): only ws-fill can archive on 2nd GET!
        clock.advance(70)
        assert adapter.reconcile().reconciled
        buckets = adapter.classify_orders()
        assert 'ws-fill' in buckets['archived_exchange']
        assert 'write-ack-cancel' in buckets['economic_incomplete']
        assert 'pending-cancel' in buckets['economic_incomplete']

        # Advance another 60s (130s > 120s CANCELED settle): canceled orders now archive!
        clock.advance(60)
        assert adapter.reconcile().reconciled
        buckets = adapter.classify_orders()
        assert {'ws-fill', 'write-ack-cancel', 'pending-cancel'} <= set(buckets['archived_exchange'])
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 7. Fault: Clock rollback
# ---------------------------------------------------------------------------

def test_f12_clock_rollback_prevents_premature_archive_and_forces_audit(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='rb-order', order_id=1, status='FILLED', filled_qty=D('1'))
        # 1st GET at NOW.
        assert adapter.reconcile().reconciled
        assert 'rb-order' in adapter.classify_orders()['economic_incomplete']

        # Roll clock BACKWARD by 30 seconds before 2nd GET!
        clock.advance(-30)
        assert adapter.reconcile().reconciled
        # Must NOT archive under clock rollback!
        assert 'rb-order' in adapter.classify_orders()['economic_incomplete']
        assert adapter.recovery_status()['clock_rollback_count'] >= 1

        # Restore clock forward -> now archives cleanly.
        clock.advance(60)
        assert adapter.reconcile().reconciled
        assert 'rb-order' in adapter.classify_orders()['archived_exchange']

        # Run audit at T, then roll clock back -> audit_due() MUST immediately report True!
        adapter.audit_history(force=True)
        assert not adapter.audit_due()
        clock.advance(-60)
        assert adapter.audit_due() is True
        summary = adapter.audit_if_due()
        assert summary is not None and summary['clock_rollback_detected'] is True
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 8. Fault: Duplicate pages & overlapping windows
# ---------------------------------------------------------------------------

def test_f12_duplicate_pages_and_overlapping_windows_idempotent_and_conflict_safe(tmp_path):
    clock = MutableClock()

    class OverlappingPagesTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.pages = []
            self.page_idx = 0

        def __call__(self, request, timeout):
            if request.path == '/fapi/v1/userTrades' and self.pages:
                self.calls.append((request.method, request.path, parse_qs(request.query)))
                page = self.pages[min(self.page_idx, len(self.pages) - 1)]
                self.page_idx += 1
                return Response(200, {}, json.dumps(page).encode())
            return super().__call__(request, timeout)

    fake = OverlappingPagesTransport()
    t1 = dict(symbol='NVDAUSDT', id=1, orderId=1, qty='1', quoteQty='100', commission='0.1', commissionAsset='USDT', time=MS)
    t2 = dict(symbol='NVDAUSDT', id=2, orderId=2, qty='1', quoteQty='100', commission='0.1', commissionAsset='USDT', time=MS + 1)
    t3 = dict(symbol='NVDAUSDT', id=3, orderId=3, qty='1', quoteQty='100', commission='0.1', commissionAsset='USDT', time=MS + 2)
    fake.pages = [[t1, t2], [t2, t3], [t3]]

    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        rows = adapter.user_trades_paginated('NVDAUSDT', from_id=1, limit=2, max_pages=5)
        assert [r['id'] for r in rows] == [1, 2, 3]

        # Conflicting trade payload for same trade ID across overlapping pages MUST fail closed!
        t2_corrupt = dict(t2, commission='0.99')
        fake.pages = [[t1, t2], [t2_corrupt, t3]]
        fake.page_idx = 0
        with pytest.raises(DataQualityError, match='conflicting duplicate trade'):
            adapter.user_trades_paginated('NVDAUSDT', from_id=1, limit=2, max_pages=5)

        # Repeated overlapping audit windows are strictly idempotent.
        fake.pages = []
        seed_historical_order(adapter, fake, key='ov-1', order_id=10, status='FILLED', filled_qty=D('1'))
        archive_seeded_orders(adapter, clock)
        events_before = adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0]
        adapter.audit_history(force=True)
        adapter.audit_history(force=True)
        assert adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] == events_before
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 9. Fault: Mid-recovery crash & 10. Recovery state write interruption
# ---------------------------------------------------------------------------

def test_f12_mid_recovery_crash_does_not_advance_watermark_or_corrupt_state(tmp_path, monkeypatch):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='c-1', order_id=1, status='CANCELED')
        seed_historical_order(adapter, fake, key='c-2', order_id=2, status='CANCELED')
        archive_seeded_orders(adapter, clock)
        adapter.audit_history(force=True, audit_batch_size=0)
        watermark_before = adapter.recovery_status()['watermarks']['trades:NVDAUSDT']['last_trade_id']

        # Both archived orders receive late fills.
        fake.trade_update('c-1', '1', '100')
        fake.trade_update('c-2', '1', '100')

        orig_query = adapter.query_order
        calls = []

        def crash_on_second(cid):
            calls.append(cid)
            if len(calls) == 2:
                raise RuntimeError('simulated crash mid-audit')
            return orig_query(cid)

        monkeypatch.setattr(adapter, 'query_order', crash_on_second)
        with pytest.raises(RuntimeError, match='simulated crash mid-audit'):
            adapter.audit_history(force=True, audit_batch_size=0)

        # Watermark MUST NOT have advanced past the unbooked 2nd order!
        assert adapter.recovery_status()['watermarks']['trades:NVDAUSDT']['last_trade_id'] == watermark_before

        # Restore and retry: idempotent for c-1, completes c-2, then advances watermark!
        monkeypatch.setattr(adapter, 'query_order', orig_query)
        adapter.audit_history(force=True, audit_batch_size=0)
        assert adapter.order('c-1').status is S.FILLED
        assert adapter.order('c-2').status is S.FILLED
        assert adapter.recovery_status()['watermarks']['trades:NVDAUSDT']['last_trade_id'] == 2
        assert adapter.reconcile().reconciled
    finally:
        adapter.store.close()


def test_f12_recovery_state_write_interruption_rolls_back_atomically(tmp_path, monkeypatch):
    from factorlab.integrations.binance_futures_trading import adapter as adapter_mod

    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='atomic-1', order_id=1, status='NEW')
        assert adapter.reconcile().reconciled
        fake.trade_update('atomic-1', '1', '100')
        before_order = adapter._load('atomic-1')
        before_events = adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0]
        before_receipts = adapter.db.execute('SELECT count(*) FROM futures_receipts').fetchone()[0]

        def fail_recovery_write(*args, **kwargs):
            raise RuntimeError('interrupted recovery metadata write')

        monkeypatch.setattr(adapter_mod, 'sync_order_recovery_on_apply', fail_recovery_write)
        with pytest.raises(RuntimeError, match='interrupted recovery metadata write'):
            adapter.query_order('atomic-1')

        # SQLite rolled back order, event, receipt, and recovery metadata atomically!
        assert adapter._load('atomic-1') == before_order
        assert adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] == before_events
        assert adapter.db.execute('SELECT count(*) FROM futures_receipts').fetchone()[0] == before_receipts
        assert not adapter.store.db.in_transaction
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 11. Fault: Independent process restart preserves archives & idempotence
# ---------------------------------------------------------------------------

def test_f12_independent_process_restart_preserves_archives_and_idempotence(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    fake.cash = D('10000')
    protocol_path = tmp_path / 'protocol.sqlite'
    controller_path = tmp_path / 'controller.sqlite'
    adapter = make_adapter(protocol_path, fake, clock)
    gateway, controller = make_gateway_stack(adapter, controller_path, clock, cash=D('10000'))
    try:
        for i in range(10):
            seed_historical_order(
                adapter, fake, key=f'arch-{i}', order_id=i + 1,
                status='FILLED' if i < 5 else 'CANCELED',
                filled_qty=D('1') if i < 5 else D('0'),
                side=Side.BUY if i % 2 == 0 else Side.SELL,
            )
        seed_historical_order(adapter, fake, key='active-1', order_id=99, status='NEW', update_time_ms=MS)
        # First pass imports and replays all orders; second pass archives the 10 historical ones.
        assert gateway.recover(controller).reconciled
        clock.advance(2)
        assert gateway.recover(controller).reconciled
        assert len(adapter.classify_orders()['archived_exchange']) == 10
        assert adapter.classify_orders()['active'] == ('active-1',)
        expected_events = dumps(list(controller.store.order_events()))
        expected_account = dict(
            cash=str(fake.cash), qty=str(fake.quantity), entry=str(fake.entry),
            orders=fake.orders, trades=fake.trades,
        )
    finally:
        controller.store.close()
        adapter.store.close()

    program = '''
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal as D
import json, sys
sys.path[:0] = sys.argv[3:5]
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
from factorlab.integrations.binance_futures_trading.protocol import Credentials, FuturesClient
from factorlab.integrations.binance_futures_trading.fake import synthetic_rules
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore, dumps

state = json.load(sys.stdin)
fake = FakeTransport()
fake.orders, fake.trades = state['orders'], state['trades']
fake.cash, fake.quantity, fake.entry = D(state['cash']), D(state['qty']), D(state['entry'])
now = datetime.fromisoformat(sys.argv[5])
client = FuturesClient(Credentials('fixture-key', 'fixture-secret'), transport=fake, clock=lambda: now.timestamp())
adapter = FuturesAdapter(client, PaperStore(sys.argv[1], continuous_capacity=True), account_id='offline', clock=lambda: now)
gateway = FuturesPaperGateway(adapter, synthetic_rules(), (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('10000')})
controller = TradingController(gateway, PaperStore(sys.argv[2], continuous_capacity=True),
                               clock=lambda: now, limits=RiskLimits(D('50000'), D('50000'), D('80000'), 1000))
controller.quote(Quote('NVDAUSDT', D('100'), now))
try:
    assert len(adapter.classify_orders()['archived_exchange']) == 10
    assert adapter.classify_orders()['unknown'] == ('active-1',)
    account = gateway.recover(controller)
    assert account.reconciled
    counts = Counter(path for m, path, _ in fake.calls if m == 'GET')
    # Only the 1 active order was queried; all 10 archived orders were skipped across restart!
    assert counts['/fapi/v1/order'] == 1
    assert counts['/fapi/v1/userTrades'] == 0
    assert dumps(list(controller.store.order_events())) == state['expected_events']
    assert account.assets[0].total == fake.cash
    assert sum((p.quantity for p in account.positions), D('0')) == fake.quantity
    print('SUBPROCESS_RESTART_OK')
finally:
    controller.store.close()
    adapter.store.close()
'''
    payload = dict(expected_account, expected_events=expected_events)
    src_root = str(tmp_path)  # unused placeholder; pass actual repo src below
    import factorlab
    from pathlib import Path
    repo_src = str(Path(factorlab.__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, '-c', program, str(protocol_path), str(controller_path),
         repo_src, repo_src, clock.utc().isoformat()],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.strip() == 'SUBPROCESS_RESTART_OK'


# ---------------------------------------------------------------------------
# 12. Fault: Historical audit detects inconsistent persisted record
# ---------------------------------------------------------------------------

def test_f12_historical_audit_persisted_inconsistency_blocks_trading_gate(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='tampered-1', order_id=1, status='FILLED', filled_qty=D('1'))
        archive_seeded_orders(adapter, clock)
        assert adapter.reconcile().reconciled

        # Tamper persisted futures_order_recovery fee so it disagrees with futures_orders/receipt!
        with adapter.store.transaction():
            adapter.db.execute(
                "UPDATE futures_order_recovery SET confirmed_fee='0.999' WHERE client_order_id='tampered-1'"
            )

        with pytest.raises(DataQualityError, match='inconsistent'):
            adapter.audit_history(force=True, audit_batch_size=5)
        assert not adapter.reconciled
        assert adapter.order('tampered-1').reconcile_required
        assert adapter.recovery_status()['history_audit_status'] == 'CONFLICT'
        with pytest.raises(SafetyBlocked):
            adapter.submit_order(make_intent('blocked-by-audit'))
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# 13. Fault: Account openOrders mismatch + F11 priority + F10 income path
# ---------------------------------------------------------------------------

def test_f12_open_orders_mismatch_reopens_archived_or_blocks_unmanaged(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='arch-open', order_id=1, status='FILLED', filled_qty=D('1'))
        archive_seeded_orders(adapter, clock)
        assert 'arch-open' in adapter.classify_orders()['archived_exchange']

        # Exchange contradicts local archive by listing arch-open in openOrders!
        fake.orders['arch-open']['status'] = 'NEW'
        with pytest.raises((DataQualityError, ValueError)):
            adapter.reconcile()
        assert not adapter.reconciled
        assert adapter.order('arch-open').reconcile_required
        assert 'arch-open' not in adapter.classify_orders()['archived_exchange']

        # Repair arch-open, then inject an unmanaged foreign open order.
        fake.orders['arch-open']['status'] = 'FILLED'
        assert adapter.reconcile().reconciled
        fake.orders['foreign-manual'] = dict(
            clientOrderId='foreign-manual', orderId=999, symbol='NVDAUSDT',
            side='BUY', type='LIMIT', positionSide='BOTH', origQty='1',
            executedQty='0', cumQuote='0', status='NEW', updateTime=MS,
        )
        with pytest.raises(DataQualityError, match='unmanaged open orders'):
            adapter.reconcile()
        assert not adapter.reconciled
    finally:
        adapter.store.close()


def test_f12_active_reduce_only_sell_priority_and_independent_funding_income(tmp_path):
    clock = MutableClock()
    fake = FakeTransport()
    fake.income = [
        {'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.02', 'asset': 'USDT', 'time': MS, 'tranId': 101},
        {'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.03', 'asset': 'USDT', 'time': MS + 1000, 'tranId': 102},
        {'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '0.01', 'asset': 'USDT', 'time': MS + 2000, 'tranId': 103},
    ]
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock)
    try:
        seed_historical_order(adapter, fake, key='active-buy', order_id=1, status='NEW', side=Side.BUY, update_time_ms=MS)
        seed_historical_order(adapter, fake, key='active-reduce-sell', order_id=2, status='NEW',
                              side=Side.SELL, reduce_only=True, update_time_ms=MS)
        assert adapter.reconcile().reconciled
        # F11 compatibility: active reduce-only SELL is prioritized ahead of BUY!
        assert adapter.classify_orders()['active'] == ('active-reduce-sell', 'active-buy')

        clock.advance(2)
        result = adapter.sync_funding(page_limit=2)
        assert [event.tran_id for event in result.newly_recorded] == ['101', '102', '103']

        audit = adapter.audit_history(force=True, include_income=True, audit_batch_size=0)
        assert audit['income_sync']['newly_recorded'] == 0
        assert adapter.funding_events() == result.events
        assert audit['income_sync']['covered_through_ms'] == adapter.funding_coverage()['covered_through_ms']
        # Funding NEVER leaks into order fees!
        assert adapter.order('active-buy').cumulative_fee == D('0')
        assert adapter.order('active-reduce-sell').cumulative_fee == D('0')
    finally:
        adapter.store.close()


# ---------------------------------------------------------------------------
# Scale invariant verification (6 / 100 / 1000 historical orders)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('size', [6, 100, 1000])
def test_f12_steady_state_recovery_is_independent_of_archived_history_size(tmp_path, size):
    clock = MutableClock()
    fake = FakeTransport()
    fake.cash = D('500000')
    config = OrderRecoveryConfig(history_audit_interval_rounds=50, history_audit_batch_size=5)
    adapter = make_adapter(tmp_path / 'protocol.sqlite', fake, clock, recovery_config=config)
    gateway, controller = make_gateway_stack(adapter, tmp_path / 'controller.sqlite', clock, cash=D('500000'))
    try:
        # Seed `size` historical terminal orders (mix of FILLED with fees and CANCELED) + 1 active + 1 partial.
        with adapter.store.transaction():
            for i in range(size):
                is_filled = (i % 2 == 0)
                side = Side.BUY if (i // 2) % 2 == 0 else Side.SELL
                seed_historical_order(
                    adapter, fake, key=f'hist-{i}', order_id=i + 1,
                    status='FILLED' if is_filled else 'CANCELED',
                    filled_qty=D('1') if is_filled else D('0'),
                    side=side, update_time_ms=MS - 3600 * 1000 + i,
                    commit=False,
                )
            # Active open order + partially filled order.
            seed_historical_order(adapter, fake, key='active-open', order_id=size + 10, status='NEW', update_time_ms=MS, commit=False)
            seed_historical_order(adapter, fake, key='active-partial', order_id=size + 11, status='NEW', update_time_ms=MS, commit=False)
        fake.trade_update('active-partial', '0.5', '100')

        # Bootstrap & archive historical orders (2 authoritative confirmations).
        assert gateway.recover(controller).reconciled
        clock.advance(2)
        assert gateway.recover(controller).reconciled
        buckets = adapter.classify_orders()
        assert len(buckets['archived_exchange']) == size
        assert set(buckets['active']) == {'active-open', 'active-partial'}

        # Steady-state real-time recovery round: MUST NOT scale with `size`!
        start_calls = len(fake.calls)
        clock.advance(2)
        account = gateway.recover(controller)
        assert account.reconciled
        assert (clock.utc() - adapter.facts.observed_at).total_seconds() <= ACCOUNT_READ_SECONDS
        counts = Counter(path for m, path, _ in fake.calls[start_calls:] if m == 'GET')
        # Exactly 2 /fapi/v1/order (active-open + active-partial) and 1 /fapi/v1/userTrades (active-partial)!
        assert counts['/fapi/v1/order'] == 2
        assert counts['/fapi/v1/userTrades'] == 1
        assert sum(counts.values()) == 8  # 2 order + 1 userTrades + 5 offline account endpoints!
        assert adapter.last_reconcile_summary['archived_skipped'] == size
        assert all(m == 'GET' for m, _, _ in fake.calls)
    finally:
        controller.store.close()
        adapter.store.close()
