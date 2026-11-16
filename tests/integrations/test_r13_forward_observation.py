from datetime import timedelta
import json

import pytest

from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.forward_observation import (
    ObservationFull, ShadowConsumer, ShadowEvidence, main)
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
from test_binance_demo_grid_bridge import bridge
from test_demo_grid_supervisor import Control, bar
from test_demo_market_feed import make, Consumer
from test_forward_observation import setup, book


def test_real_bar_receipt_precedes_decision_and_preserves_raw(tmp_path):
    class Receipts(Consumer):
        def observe_bar_receipt(self, bar, raw, received_at, known_at):
            self.receipt = (bar, raw, received_at, known_at)

        def closed_bar(self, bar):
            assert self.receipt[0] == bar
            assert self.receipt[2] <= self.receipt[3]
            super().closed_bar(bar)

    consumer = Receipts()
    feed, transport, _ = make(tmp_path, consumer=consumer)
    try:
        feed.poll()
        bar_value, raw, received, known = consumer.receipt
        assert raw[0] == int(bar_value.opened_at.timestamp() * 1000)
        assert raw[6] + 1 == int(bar_value.closed_at.timestamp() * 1000)
        assert received >= bar_value.closed_at
    finally:
        feed.close()


def test_r13_only_generates_from_bars_opened_after_freeze(tmp_path):
    b, fake = bridge()
    b.preview = True
    clock = [NOW]
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture', clock=lambda: clock[0])
    evidence.prospective_start = NOW + timedelta(seconds=1)
    consumer = ShadowConsumer(b, Control(), evidence, clock=lambda: clock[0])
    consumer.before_poll()
    clock[0] = NOW + timedelta(minutes=1)
    consumer.closed_bar(bar(1))
    assert not evidence.proposals
    clock[0] = NOW + timedelta(minutes=2)
    current = bar(2)
    raw = [int(current.opened_at.timestamp() * 1000), '150', '151', '149', '150', '1',
           int(current.closed_at.timestamp() * 1000) - 1]
    consumer.observe_bar_receipt(current, raw, clock[0], clock[0])
    consumer.closed_bar(current)
    begin = next(e for e in evidence.rows if e['kind'] == 'begin')
    assert begin['bar_receipt']['opened_at'] == current.opened_at.isoformat()
    assert 'raw' not in begin['bar_receipt']
    assert len(evidence.proposals) == 1
    assert evidence.bar_receipt is None
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_r13_quote_gap_and_rejected_book_stop_instead_of_retry(tmp_path):
    _, _, clock, evidence, consumer = setup(tmp_path)
    evidence.prospective_start = NOW
    evidence.max_quote_gap = 30
    consumer.observe_book(book(clock[0]), clock[0])
    clock[0] += timedelta(seconds=31)
    with pytest.raises(FeedPaused, match='R13_QUOTE_GAP'):
        consumer.observe_book(book(clock[0]), clock[0])
    clock[0] += timedelta(seconds=1)
    with pytest.raises(FeedPaused, match='STALE_OR_FUTURE_BOOK'):
        consumer.observe_book(book(clock[0] - timedelta(seconds=6)), clock[0])


def test_deadline_effective_end_is_separate_from_late_receipt(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from factorlab.integrations.binance_futures_trading import forward_observation as observation

    _, _, _, captured, _ = setup(tmp_path / 'fixture')
    proposal = dict(next(iter(captured.proposals.values())))
    clock = [NOW]
    monkeypatch.setattr(observation, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(observation, 'DemoCredentialStore', lambda: None)

    @contextmanager
    def expired(*args, request_counts, **kwargs):
        request_counts.evidence.append(dict(kind='capture', source=proposal['source'], proposals=[proposal]))
        clock[0] = NOW + timedelta(seconds=601)
        request_counts.before_request('feed', 'GET', '/fapi/v1/time')
        yield

    monkeypatch.setattr(observation, 'open_grid_session', expired)
    output = tmp_path / 'deadline'
    main(['--output', str(output), '--version', 'fixture', '--experiment-id', 'deadline-test',
          '--seconds', '600', '--window-seconds', '600', '--max-requests', '600'])
    from factorlab.experiments.grid_forward_observation import read_events
    rows = read_events(output / 'events.csv')
    terminal = next(e for e in rows if e['kind'] == 'end')
    assert terminal['reason'] == 'DEADLINE'
    assert terminal['at'] == (NOW + timedelta(seconds=600)).isoformat()
    receipt = next(e for e in rows if e['kind'] == 'window_end')
    assert receipt['at'] == clock[0].isoformat()
    assert json.loads((output / 'summary.json').read_text())['termination'] == 'DEADLINE'


def test_r13_restart_refused_before_network_and_retains_gap(tmp_path):
    output = tmp_path / 'window'
    evidence = ShadowEvidence(output / 'events.csv', version='fixture', window=600,
                              max_bytes=5242880 // 3 - 65536)
    evidence.append(dict(kind='experiment', parameters={}, started_at=NOW.isoformat(),
                         deadline=(NOW + timedelta(seconds=600)).isoformat()))
    with pytest.raises(ObservationFull, match='restart prohibited'):
        main(['--output', str(output), '--version', 'fixture', '--experiment-id', 'r13-fixture',
              '--seconds', '600', '--window-seconds', '600', '--max-requests', '600'])
    import csv
    with evidence.path.open(newline='') as stream:
        rows = [json.loads(r['event']) for r in csv.DictReader(stream)]
    assert any(e['kind'] == 'resume' for e in rows)
    assert not any(e['kind'] == 'request' for e in rows)
