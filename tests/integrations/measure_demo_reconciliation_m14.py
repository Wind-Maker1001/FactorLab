"""Explicit offline reconciliation cost measurements."""
from collections import Counter
from decimal import Decimal as D
import json
from time import perf_counter, process_time

import pytest

from factorlab.integrations.binance_futures_trading.fake import FakeTransport
from test_demo_reconciliation_m14 import adapter_at, historical


@pytest.mark.parametrize('size,rounds,late', [(100, 1, False), (1000, 1, False), (100, 20, False), (100, 1, True)])
def test_frozen_full_reconciliation_cost(tmp_path, size, rounds, late):
    import psutil
    from test_binance_futures_trading import gateway_setup
    fake = FakeTransport()
    adapter = adapter_at(tmp_path / 'protocol.sqlite', fake)
    for index in range(size):
        historical(adapter, fake, key=f'history-{index}', index=index + 1)
    gateway, controller = gateway_setup(adapter, tmp_path / 'controller.sqlite')
    def restore_history():
        # Frozen history represents already booked cancellations, not concurrent
        # unaccepted intents reserving collateral during orphan import.
        gateway.state.orders.update({f'history-{index}': adapter._load(f'history-{index}')[0]
                                     for index in range(size)})
    controller._atomic(restore_history)
    try:
        assert gateway.recover(controller).reconciled
        if late:
            fake.trade_update('history-0', '2')
        before_state = controller.store.load()
        before_events = controller.store.db.execute('SELECT count(*) FROM events').fetchone()[0]
        before_protocol = adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0]
        def disk_bytes():
            return sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
        before_bytes = disk_bytes()
        process = psutil.Process()
        before_rss = process.memory_info().rss
        start_calls = len(fake.calls)
        cpu, wall = process_time(), perf_counter()
        for _ in range(rounds):
            assert gateway.recover(controller).reconciled
        elapsed, cpu = perf_counter() - wall, process_time() - cpu
        counts = Counter(path for method, path, _ in fake.calls[start_calls:] if method == 'GET')
        assert counts['/fapi/v1/order'] == 2 * size * rounds
        assert sum(counts.values()) == rounds * (2 * size + 10) + (2 if late else 0)
        after_state = controller.store.load()
        if late:
            assert after_state != before_state
            assert adapter.order('history-0').cumulative_fee == D('.2')
        else:
            assert after_state == before_state
        report = dict(orders=size, rounds=rounds, late_fill=late, get_total=sum(counts.values()),
            endpoints=dict(counts), seconds=elapsed, cpu_seconds=cpu,
            rss_before=before_rss, rss_after=process.memory_info().rss,
            durable_bytes_delta=disk_bytes() - before_bytes,
            controller_events_delta=controller.store.db.execute('SELECT count(*) FROM events').fetchone()[0] - before_events,
            protocol_events_delta=adapter.db.execute('SELECT count(*) FROM futures_events').fetchone()[0] - before_protocol)
        gateway.recover(controller)
        assert controller.store.load() == after_state
        report['repeated_economic_difference'] = False
        print('M14_MEASUREMENT=' + json.dumps(report, sort_keys=True))
        assert all(method == 'GET' for method, _, _ in fake.calls)
    finally:
        controller.store.close()
        adapter.store.close()
