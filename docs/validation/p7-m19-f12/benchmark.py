"""Deterministic offline 6 / 100 / 1000 historical order benchmark for P7-M19-F12.

Compares pre-F12 O(N) full-history double-pass recovery against F12 two-tier
incremental recovery and historical audit on identical temporary SQLite stores
and deterministic FakeTransport datasets. NO LIVE MONEY.

Usage:
    python docs/validation/p7-m19-f12/benchmark.py --output docs/validation/p7-m19-f12/measurements.json
"""
import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter, process_time

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from factorlab.integrations.binance_futures_trading.adapter import ACCOUNT_READ_SECONDS, FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, MS, NOW, synthetic_rules
from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
from factorlab.integrations.binance_futures_trading.protocol import Credentials, FuturesClient, OutcomeUnknown
from factorlab.integrations.binance_futures_trading.recovery import OrderRecoveryConfig
from factorlab.trading import Market, OrderIntent, OrderSnapshot, OrderStatus as S, OrderType, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.state import OrderRecord
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


def make_intent(key, *, side=Side.BUY, limit='100', quantity='1'):
    return OrderIntent(
        Market.BINANCE_USDM, 'NVDAUSDT', side, OrderType.LIMIT,
        D(quantity), D(limit), key, position_side='BOTH', reduce_only=False,
    )


def seed_dataset(adapter, fake, size, *, clock):
    """Build deterministic dataset with filled, canceled, partial-canceled, active, partial-active."""
    fake.cash = D('500000')
    base_ms = MS - 3600 * 1000  # 1 hour old (past settle windows)
    first_canceled_key = None

    with adapter.store.transaction():
        for i in range(size):
            key = f'hist-{i:04d}'
            order_id = i + 1
            # Mix: even indices FILLED (alternating BUY/SELL), odd indices CANCELED
            # (every 3rd canceled order has a 0.5 partial fill before cancel).
            if i % 2 == 0:
                status = 'FILLED'
                filled = D('1')
                side = Side.BUY if (i // 2) % 2 == 0 else Side.SELL
            else:
                status = 'CANCELED'
                if first_canceled_key is None:
                    first_canceled_key = key
                    filled = D('0')  # Reserve first canceled order for late-fill injection
                    side = Side.BUY
                elif (i // 2) % 3 == 2:
                    filled = D('0.5')
                    side = Side.BUY if (i // 2) % 2 == 0 else Side.SELL
                else:
                    filled = D('0')
                    side = Side.BUY

            price = D('100')
            quote = filled * price
            fee = quote * D('0.001') if filled > 0 else D('0')
            update_ms = base_ms + i * 10
            updated_dt = datetime.fromtimestamp(update_ms / 1000, timezone.utc)
            mapped = S.FILLED if status == 'FILLED' else S.CANCELED
            intent = make_intent(key, side=side, limit=str(price), quantity='1')
            snap = replace(
                OrderSnapshot(intent), status=mapped,
                cumulative_filled_quantity=filled, cumulative_fee=fee,
                exchange_order_id=str(order_id),
            )
            adapter._save(OrderRecord(snap, updated_dt, price, mapped), quote)
            fake.orders[key] = dict(
                clientOrderId=key, orderId=order_id, symbol='NVDAUSDT',
                side=side.value, type='LIMIT', positionSide='BOTH',
                origQty='1', executedQty=str(filled), cumQuote=str(quote),
                status=status, updateTime=update_ms,
            )
            if filled > 0:
                tid = len(fake.trades) + 1
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

        # Add 1 active open order + 1 active partially filled order.
        for offset, (key, fill_now) in enumerate([('active-open', False), ('active-partial', True)], start=1):
            order_id = size + offset
            intent = make_intent(key, side=Side.BUY, limit='100', quantity='1')
            snap = replace(OrderSnapshot(intent), status=S.SUBMITTED, exchange_order_id=str(order_id))
            adapter._save(OrderRecord(snap, NOW, D('100'), S.SUBMITTED), D('0'))
            fake.orders[key] = dict(
                clientOrderId=key, orderId=order_id, symbol='NVDAUSDT',
                side='BUY', type='LIMIT', positionSide='BOTH',
                origQty='1', executedQty='0', cumQuote='0',
                status='NEW', updateTime=MS,
            )
            if fill_now:
                fake.trade_update(key, '0.5', '100')

    return first_canceled_key


def bind_baseline_behavior(adapter, gateway):
    """Force exact pre-F12 O(N) full-history double-pass behavior on adapter & gateway."""
    orig_reconcile = adapter.reconcile
    adapter.reconcile = lambda *, full_history=True: orig_reconcile(full_history=True)
    gateway._consume_reusable_exchange = lambda: adapter.reconcile(full_history=True)


def economic_snapshot(adapter, controller, fake):
    account = controller.account()
    events = list(controller.store.order_events())
    orders = [adapter.order(row[0]) for row in adapter.db.execute('SELECT id FROM futures_orders ORDER BY rowid')]
    total_filled = sum((o.cumulative_filled_quantity for o in orders), D('0'))
    total_fee = sum((o.cumulative_fee for o in orders), D('0'))
    pos_qty = sum((p.quantity for p in account.positions), D('0'))
    wallet = next(a.total for a in account.assets if a.currency == 'USDT')
    unrealized = sum((p.quantity * (p.mark_price - p.entry_price) for p in account.positions), D('0'))
    equity = wallet + unrealized
    return dict(
        reconciled=account.reconciled,
        controller_order_events=len(events),
        controller_fill_events=sum(1 for _, e in events if e.fill_price is not None),
        protocol_events=adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0],
        total_filled_quantity=str(total_filled),
        total_fee_usdt=str(total_fee),
        position_qty=str(pos_qty),
        wallet_usdt=str(wallet),
        equity_usdt=str(equity),
        exchange_wallet_usdt=str(fake.cash),
        exchange_position_qty=str(fake.quantity),
        events_digest=dumps(events),
    )


def measure_mode(size, *, mode, steady_rounds=5):
    """Run one complete measurement for `mode` in ('baseline_on', 'optimized_f12')."""
    with tempfile.TemporaryDirectory(prefix=f'f12-{mode}-{size}-') as tmp:
        root = Path(tmp)
        protocol_path = root / 'protocol.sqlite'
        controller_path = root / 'controller.sqlite'
        clock = MutableClock()
        fake = FakeTransport()
        config = OrderRecoveryConfig(
            history_audit_interval_rounds=1000,  # Trigger scheduled audit explicitly to separate costs cleanly.
            history_audit_interval_seconds=3600,
            history_audit_batch_size=5,
        )
        client = FuturesClient(Credentials('fixture-key', 'fixture-secret'), transport=fake, clock=clock.epoch)
        adapter = FuturesAdapter(client, PaperStore(protocol_path, continuous_capacity=True, research_wal=True),
                                 account_id='offline', clock=clock.utc, recovery_config=config)
        gateway = FuturesPaperGateway(adapter, synthetic_rules(),
                                      (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('500000')})
        controller = TradingController(gateway, PaperStore(controller_path, continuous_capacity=True, research_wal=True),
                                       clock=clock.utc, limits=RiskLimits(D('500000'), D('500000'), D('800000'), 10000))
        controller.quote(Quote('NVDAUSDT', D('100'), clock.utc()))
        try:
            late_target = seed_dataset(adapter, fake, size, clock=clock)
            # Bootstrap & confirm all seeded orders so both modes start from identical confirmed steady state.
            assert gateway.recover(controller).reconciled
            clock.advance(2)
            assert gateway.recover(controller).reconciled
            assert len(adapter.classify_orders()['archived_exchange']) == size
            # Seed initial trade watermark so audit round measures true steady-state incremental audit.
            adapter.audit_history(force=True, audit_batch_size=0)

            if mode == 'baseline_on':
                bind_baseline_behavior(adapter, gateway)

            # ---- 1. Steady-state real-time polling rounds ----
            steady_call_start = len(fake.calls)
            t0, c0 = perf_counter(), process_time()
            freshness_ages = []
            for _ in range(steady_rounds):
                clock.advance(1)
                assert gateway.recover(controller).reconciled
                freshness_ages.append((clock.utc() - adapter.facts.observed_at).total_seconds())
            steady_wall = perf_counter() - t0
            steady_cpu = process_time() - c0
            steady_counts = Counter(path for m, path, _ in fake.calls[steady_call_start:] if m == 'GET')

            # ---- 2. UNKNOWN (lost POST ACK) recovery ----
            controller.quote(Quote('NVDAUSDT', D('100'), clock.utc()))
            fake.timeout_next = True
            unknown_intent = make_intent('unknown-ack-1', side=Side.BUY, limit='100', quantity='1')
            assert controller.submit(unknown_intent) is None
            assert adapter.order('unknown-ack-1').status is S.UNKNOWN
            unk_start = len(fake.calls)
            ut0 = perf_counter()
            assert gateway.recover(controller).reconciled
            unk_wall = perf_counter() - ut0
            unk_counts = Counter(path for m, path, _ in fake.calls[unk_start:] if m == 'GET')
            unknown_status = adapter.order('unknown-ack-1').status.value
            post_total = sum(1 for m, _, _ in fake.calls if m == 'POST')
            assert post_total == 1 and unknown_status == 'SUBMITTED'

            # ---- 3. Late fill on archived historical CANCELED order ----
            fake.trade_update(late_target, '1', '100')
            late_start = len(fake.calls)
            lt0 = perf_counter()
            assert gateway.recover(controller).reconciled
            late_wall = perf_counter() - lt0
            late_counts = Counter(path for m, path, _ in fake.calls[late_start:] if m == 'GET')
            assert adapter.order(late_target).status is S.FILLED
            assert adapter.order(late_target).cumulative_fee == D('0.1')

            # ---- 4. Separate Tier-2 scheduled historical audit cost (optimized only) ----
            audit_report = None
            if mode == 'optimized_f12':
                audit_start = len(fake.calls)
                at0 = perf_counter()
                audit_summary = adapter.audit_history(force=True)
                audit_wall = perf_counter() - at0
                audit_counts = Counter(path for m, path, _ in fake.calls[audit_start:] if m == 'GET')
                audit_report = dict(
                    get_total=sum(audit_counts.values()),
                    order_gets=audit_counts['/fapi/v1/order'],
                    user_trades_gets=audit_counts['/fapi/v1/userTrades'],
                    wall_seconds=round(audit_wall, 6),
                    trades_scanned=audit_summary['trades_scanned'],
                    rolling_orders_audited=audit_summary['rolling_orders_audited'],
                    overlap_ids=config.trade_overlap_ids,
                    batch_size=config.history_audit_batch_size,
                    full_rolling_cycle_audits=(size + config.history_audit_batch_size - 1) // config.history_audit_batch_size,
                )

            econ_before_restart = economic_snapshot(adapter, controller, fake)
        finally:
            controller.store.close()
            adapter.store.close()

        # ---- 5. Independent reopen / restart idempotence ----
        clock.advance(2)
        client2 = FuturesClient(Credentials('fixture-key', 'fixture-secret'), transport=fake, clock=clock.epoch)
        adapter2 = FuturesAdapter(client2, PaperStore(protocol_path, continuous_capacity=True, research_wal=True),
                                  account_id='offline', clock=clock.utc, recovery_config=config)
        gateway2 = FuturesPaperGateway(adapter2, synthetic_rules(),
                                       (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('500000')})
        controller2 = TradingController(gateway2, PaperStore(controller_path, continuous_capacity=True, research_wal=True),
                                        clock=clock.utc, limits=RiskLimits(D('500000'), D('500000'), D('800000'), 10000))
        controller2.quote(Quote('NVDAUSDT', D('100'), clock.utc()))
        try:
            if mode == 'baseline_on':
                bind_baseline_behavior(adapter2, gateway2)
            r_start = len(fake.calls)
            rt0 = perf_counter()
            assert gateway2.recover(controller2).reconciled
            restart_wall = perf_counter() - rt0
            restart_counts = Counter(path for m, path, _ in fake.calls[r_start:] if m == 'GET')
            econ_after_restart = economic_snapshot(adapter2, controller2, fake)
            restart_idempotent = (
                econ_after_restart['events_digest'] == econ_before_restart['events_digest']
                and econ_after_restart['total_filled_quantity'] == econ_before_restart['total_filled_quantity']
                and econ_after_restart['total_fee_usdt'] == econ_before_restart['total_fee_usdt']
                and econ_after_restart['position_qty'] == econ_before_restart['position_qty']
                and econ_after_restart['wallet_usdt'] == econ_before_restart['wallet_usdt']
                and econ_after_restart['equity_usdt'] == econ_before_restart['equity_usdt']
            )
            assert restart_idempotent
            recovery_meta = adapter2.recovery_status()
        finally:
            controller2.store.close()
            adapter2.store.close()

        return dict(
            mode=mode,
            historical_orders=size,
            steady_rounds=steady_rounds,
            steady_per_round=dict(
                get_total=sum(steady_counts.values()) / steady_rounds,
                order_gets=steady_counts['/fapi/v1/order'] / steady_rounds,
                user_trades_gets=steady_counts['/fapi/v1/userTrades'] / steady_rounds,
                account_gets=(sum(steady_counts.values()) - steady_counts['/fapi/v1/order'] - steady_counts['/fapi/v1/userTrades']) / steady_rounds,
                wall_seconds=round(steady_wall / steady_rounds, 6),
                cpu_seconds=round(steady_cpu / steady_rounds, 6),
                max_snapshot_age_seconds=max(freshness_ages),
                within_5s_freshness=all(0 <= age <= ACCOUNT_READ_SECONDS for age in freshness_ages),
            ),
            unknown_recovery=dict(
                get_total=sum(unk_counts.values()),
                order_gets=unk_counts['/fapi/v1/order'],
                user_trades_gets=unk_counts['/fapi/v1/userTrades'],
                wall_seconds=round(unk_wall, 6),
                recovered_status=unknown_status,
                post_count=post_total,
            ),
            late_fill_recovery=dict(
                target_order=late_target,
                get_total=sum(late_counts.values()),
                order_gets=late_counts['/fapi/v1/order'],
                user_trades_gets=late_counts['/fapi/v1/userTrades'],
                wall_seconds=round(late_wall, 6),
            ),
            scheduled_history_audit=audit_report,
            restart_recovery=dict(
                get_total=sum(restart_counts.values()),
                order_gets=restart_counts['/fapi/v1/order'],
                user_trades_gets=restart_counts['/fapi/v1/userTrades'],
                wall_seconds=round(restart_wall, 6),
                idempotent=restart_idempotent,
            ),
            economics={k: v for k, v in econ_after_restart.items() if k != 'events_digest'},
            recovery_status=recovery_meta,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sizes', nargs='+', type=int, default=[6, 100, 1000])
    parser.add_argument('--rounds', type=int, default=5)
    args = parser.parse_args()

    results = []
    for size in args.sizes:
        steady_rounds = args.rounds if size < 1000 else 1
        baseline = measure_mode(size, mode='baseline_on', steady_rounds=steady_rounds)
        optimized = measure_mode(size, mode='optimized_f12', steady_rounds=steady_rounds)
        # Assert exact economic parity between original O(N) and optimized F12!
        for field in ('reconciled', 'controller_fill_events', 'total_filled_quantity',
                      'total_fee_usdt', 'position_qty', 'wallet_usdt', 'equity_usdt'):
            assert baseline['economics'][field] == optimized['economics'][field], (
                f'Parity mismatch at size={size} field={field}: '
                f"{baseline['economics'][field]} != {optimized['economics'][field]}"
            )
        b_get = baseline['steady_per_round']['get_total']
        o_get = optimized['steady_per_round']['get_total']
        b_wall = baseline['steady_per_round']['wall_seconds']
        o_wall = optimized['steady_per_round']['wall_seconds']
        results.append(dict(
            historical_orders=size,
            baseline=baseline,
            optimized=optimized,
            reduction=dict(
                steady_get_reduction_pct=round((1 - o_get / b_get) * 100, 2) if b_get else 0.0,
                steady_order_get_reduction_pct=round(
                    (1 - optimized['steady_per_round']['order_gets'] / baseline['steady_per_round']['order_gets']) * 100, 2
                ) if baseline['steady_per_round']['order_gets'] else 0.0,
                steady_wall_speedup_x=round(b_wall / o_wall, 2) if o_wall > 0 else None,
            ),
        ))

    report = dict(
        benchmark='P7-M19-F12-INCREMENTAL-RECOVERY',
        python=sys.version.split()[0],
        safety='NO_LIVE_MONEY_OFFLINE_DETERMINISTIC',
        results=results,
    )
    text = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
