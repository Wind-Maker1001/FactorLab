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
from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import wired


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


@pytest.mark.parametrize(('host_offset_seconds', 'termination'), [
    (-20, 'RUN_END'),
    (20, 'RUN_END'),
    (-20, 'DEADLINE'),
    (20, 'DEADLINE'),
    (-20, 'DATA_INTERRUPTION'),
    (20, 'CAPACITY_LIMIT'),
])
def test_preview_terminates_late_proposal_in_calibrated_window(network_grid, monkeypatch,
                                                              host_offset_seconds, termination):
    from contextlib import contextmanager
    from factorlab.experiments.grid_forward_observation import read_events
    from factorlab.integrations.binance_futures_trading import forward_observation as observation
    from factorlab.integrations.binance_futures_trading import network, protocol
    from factorlab.integrations.binance_futures_trading.demo_session import DemoSession

    root, credentials, factory, _, _, replayed_requests = network_grid
    host_offset = timedelta(seconds=host_offset_seconds)
    exchange_at = NOW
    deadline = NOW + timedelta(seconds=130)
    captured_at = bar(2).closed_at
    original_open = observation.open_grid_session

    @contextmanager
    def open_with_startup_time(*args, **kwargs):
        nonlocal exchange_at
        with original_open(*args, **kwargs) as opened:
            exchange_at += timedelta(seconds=3)
            yield opened

    monkeypatch.setattr(observation, 'utcnow', lambda: exchange_at + host_offset)
    monkeypatch.setattr(observation, 'monotonic', lambda: (exchange_at - NOW).total_seconds())
    monkeypatch.setattr(observation, 'DemoCredentialStore', lambda: credentials)
    monkeypatch.setattr(observation, 'grid_factory', lambda settings: factory)
    monkeypatch.setattr(observation, 'open_grid_session', open_with_startup_time)
    monkeypatch.setattr(observation.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(DemoSession, '_now', lambda _: exchange_at)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)

    final_request_path = '/fapi/v1/time'
    request_at = deadline - timedelta(seconds=2)

    def run(supervisor, *, seconds):
        nonlocal exchange_at
        assert seconds == pytest.approx(127)
        consumer = supervisor.before_poll.__self__
        assert consumer.evidence.prospective_start == NOW
        supervisor.before_poll()
        exchange_at = captured_at
        current = bar(2)
        assert consumer.closed_bar(current)
        proposal, = consumer.evidence.proposals.values()
        assert proposal['captured_at'] == captured_at.isoformat()
        assert timedelta(0) < deadline - captured_at < timedelta(seconds=20)

        exchange_at = request_at
        counts = supervisor.request_counts
        counts.before_request('feed', 'GET', final_request_path)
        if termination == 'DEADLINE':
            exchange_at = deadline + timedelta(seconds=1)
            counts.record('feed', 'GET', final_request_path, 3, None, {}, status=200)
            pytest.fail('late receipt must be retained and stop the observation')
        exchange_at = deadline - timedelta(seconds=1)
        counts.record('feed', 'GET', final_request_path, 1, None, {}, status=200)
        if termination == 'DATA_INTERRUPTION':
            supervisor.feed.pause('STALE_OR_FUTURE_QUOTE')
            supervisor.on_pause()
        elif termination == 'CAPACITY_LIMIT':
            raise ObservationFull('HTTP request budget exhausted')

    monkeypatch.setattr(observation.DemoSupervisor, 'run', run)
    output = root / 'calibrated-window'
    assert main(['--output', str(output), '--version', 'fixture', '--experiment-id', 'calibrated-window',
                 '--seconds', '130']) == 0
    rows = read_events(output / 'events.csv')
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    experiment, = [row for row in rows if row['kind'] == 'experiment']
    assert experiment['started_at'] == NOW.isoformat()
    assert experiment['deadline'] == deadline.isoformat()
    assert observation.instant(experiment['deadline']) - observation.instant(experiment['started_at']) == timedelta(seconds=130)
    request, = [row for row in rows if row['kind'] == 'request'
                and row['source'] == 'feed' and row['endpoint'] == final_request_path]
    receipt, = [row for row in rows if row['kind'] == 'request_result'
                and row['source'] == 'feed' and row['endpoint'] == final_request_path]
    assert request['at'] == request_at.isoformat()
    assert receipt['completed_at'] == exchange_at.isoformat()
    terminal, = [row for row in rows if row['kind'] == 'end']
    assert terminal['reason'] == termination
    assert terminal['at'] == min(exchange_at, deadline).isoformat()
    assert captured_at <= observation.instant(terminal['at']) <= deadline
    window_end, = [row for row in rows if row['kind'] == 'window_end']
    assert window_end['at'] == summary['ended_at'] == exchange_at.isoformat()
    assert summary['right_censored'] == 1
    assert all(request.get_method() == 'GET' for request in replayed_requests)


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
