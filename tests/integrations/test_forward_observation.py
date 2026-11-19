from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.forward_observation import (
    ObservationFull, ShadowConsumer, ShadowEvidence, main)
from test_binance_demo_grid_bridge import bridge
from test_demo_grid_supervisor import Control, bar
from test_demo_market_feed import make
from test_binance_demo_grid_network import network_grid
from test_binance_demo_network import wired


def setup(tmp_path, *, window=180, **kwargs):
    b, fake = bridge()
    b.preview = True
    clock = [NOW]
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture', window=window,
                              clock=lambda: clock[0], **kwargs)
    consumer = ShadowConsumer(b, Control(), evidence, clock=lambda: clock[0])
    consumer.before_poll()
    clock[0] = NOW + timedelta(minutes=1)
    consumer.closed_bar(bar(1))
    return b, fake, clock, evidence, consumer


def book(at, bid='144', ask='146', **kwargs):
    return dict(time=int(at.timestamp() * 1000), symbol='NVDAUSDT',
                bidPrice=bid, askPrice=ask, bidQty='.08', askQty='.08', **kwargs)


def test_original_proposal_baseline_and_forward_touch(tmp_path):
    b, fake, clock, evidence, consumer = setup(tmp_path)
    proposal = next(iter(evidence.proposals.values()))
    assert proposal['limit'] == '145.0'
    assert proposal['quantity'] == '.04' or D(proposal['quantity']) == D('.04')
    assert proposal['source'].endswith(str(int(clock[0].timestamp())))
    consumer.observe_book(book(clock[0], '143', '144'), clock[0])
    assert proposal['first_touch'] is None
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0]), clock[0])
    assert proposal['first_touch'] == clock[0].isoformat()
    assert proposal['first_potential'] is None
    first = proposal['first_touch']
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0], '143', '144'), clock[0])
    assert proposal['first_touch'] == first
    assert proposal['first_potential'] == clock[0].isoformat()
    assert evidence.rows[-1]['updates'][0]['target_covered_at_top']
    assert evidence.summary()['confirmed_exchange_fills'] == 0
    assert b.account().inventory == 0
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_replay_history_and_restart_identity(tmp_path):
    b, fake, clock, evidence, consumer = setup(tmp_path)
    expected = set(evidence.proposals)
    assert not consumer.closed_bar(bar(1))
    with pytest.raises(ValueError, match='observation time regression'):
        consumer.closed_bar(bar(0))
    assert set(evidence.proposals) == expected
    restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: clock[0])
    assert set(restored.proposals) == expected
    assert restored.rows[-1]['kind'] == 'resume'
    assert all(method == 'GET' for method, _, _ in fake.calls)


@pytest.mark.parametrize('cause', ['breakout', 'risk_pause', 'rebuild', 'order_expiry'])
def test_strategy_cancellation_ends_before_later_books(tmp_path, cause):
    from dataclasses import replace

    b, fake, clock, evidence, consumer = setup(tmp_path)
    changes = {
        'breakout': {},
        'risk_pause': dict(max_drawdown=0.1),
        'rebuild': dict(breakout='rebuild', min_rebuild_interval=1),
        'order_expiry': dict(order_ttl=1),
    }
    b.grid.config = replace(b.grid.config, **changes[cause])
    if cause == 'risk_pause':
        b.grid.peak = D('2000')
    event = bar(2, high=D('161'), close=D('160')) if cause in {'breakout', 'rebuild'} else bar(2)
    clock[0] = event.closed_at
    assert consumer.closed_bar(event)
    proposal = next(iter(evidence.proposals.values()))
    assert proposal['proposal'] in b.grid.cancellations
    assert proposal['proposal'] in {o.intent.client_order_id for o in b.grid.active}
    assert proposal['status'] == 'ENDED'
    assert proposal['reason'] == 'STRATEGY_INVALIDATED:' + str(b.grid.reason)
    assert proposal['ended_at'] == clock[0].isoformat()
    assert proposal['right_censored']
    frozen = dict(proposal)
    ends = [row for row in evidence.rows if row['kind'] == 'end']
    assert not consumer.closed_bar(event)
    consumer._end_inactive()
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0], '143', '144'), clock[0])
    assert proposal == frozen
    assert [row for row in evidence.rows if row['kind'] == 'end'] == ends

    restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: clock[0])
    resumed = ShadowConsumer(b, Control(), restored, clock=lambda: clock[0])
    resumed.before_poll()
    assert not resumed.closed_bar(event)
    clock[0] += timedelta(seconds=1)
    resumed.observe_book(book(clock[0], '143', '144'), clock[0])
    assert restored.proposals[proposal['id']] == frozen
    assert [row for row in restored.rows if row['kind'] == 'end'] == ends
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_pending_cancellation_keeps_other_proposals_observing(tmp_path):
    from dataclasses import replace
    from factorlab.trading import Side

    b, fake, clock, evidence, consumer = setup(tmp_path)
    original = next(iter(evidence.proposals.values()))
    b.grid.config = replace(b.grid.config, max_inventory=2, max_cash_committed=14)
    b.grid.propose(Side.BUY, D('144'), D('.04'))
    clock[0] += timedelta(seconds=1)
    event = bar(2)
    evidence.begin('second', event, True, clock[0])
    evidence.capture('second', b.grid, clock[0])
    other = next(p for p in evidence.proposals.values() if p is not original)
    b.grid.cancellations.add(original['proposal'])
    consumer._end_inactive()
    frozen = dict(original)
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0], '142', '143'), clock[0])
    assert original == frozen
    assert original['status'] == 'ENDED'
    assert other['status'] == 'OBSERVING'
    assert other['first_touch'] == clock[0].isoformat()
    assert other['first_potential'] == clock[0].isoformat()
    assert all(method == 'GET' for method, _, _ in fake.calls)


@pytest.mark.parametrize('offset,reason', [(6, 'STALE'), (-1, 'FUTURE')])
def test_reject_stale_future_book(tmp_path, offset, reason):
    _, _, clock, evidence, consumer = setup(tmp_path)
    market = clock[0] + timedelta(seconds=1)
    consumer.observe_book(book(market, '143', '144'), market + timedelta(seconds=offset))
    assert reason in evidence.rows[-1]['reason']
    assert evidence.summary()['price_touched'] == 0


def test_market_time_ordering_and_no_touch_right_censor(tmp_path):
    _, _, clock, evidence, consumer = setup(tmp_path, window=10)
    clock[0] += timedelta(seconds=1)
    raw = book(clock[0], '149', '151')
    consumer.observe_book(raw, clock[0])
    consumer.observe_book(raw, clock[0])
    assert evidence.rows[-1]['reason'] == 'NONINCREASING_BOOK'
    consumer.observe_book(book(clock[0] - timedelta(seconds=1)), clock[0])
    assert evidence.rows[-1]['reason'] == 'NONINCREASING_BOOK'
    clock[0] += timedelta(seconds=10)
    consumer.observe_book(book(clock[0], '143', '144'), clock[0])
    proposal = next(iter(evidence.proposals.values()))
    assert proposal['status'] == 'ENDED'
    assert proposal['reason'] == 'WINDOW_END'
    assert proposal['right_censored']
    assert proposal['first_touch'] is None


def test_touch_survives_restart_without_inventing_gap_touch(tmp_path):
    _, _, clock, evidence, consumer = setup(tmp_path)
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0]), clock[0])
    first = next(iter(evidence.proposals.values()))['first_touch']
    clock[0] += timedelta(seconds=20)
    restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: clock[0])
    restored.book(book(clock[0], '143', '144'), clock[0])
    proposal = next(iter(restored.proposals.values()))
    assert proposal['first_touch'] == first
    assert proposal['first_potential'] == clock[0].isoformat()
    restored.end('RUN_END')
    assert not proposal['right_censored']


def test_capture_crash_recovered_from_committed_source(tmp_path):
    b, _, clock, evidence, consumer = setup(tmp_path)
    # Simulate a crash after Grid source commit but before evidence capture.
    rows = [r for r in evidence.rows if r['kind'] != 'capture']
    import csv
    with evidence.path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['event'])
        writer.writerows([[json.dumps(row)] for row in rows])
    restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: clock[0])
    restored.recover(b)
    assert set(restored.proposals) == set(evidence.proposals)
    restored.recover(b)
    assert len(restored.proposals) == 1


def test_uncommitted_live_begin_not_promoted_on_historical_replay(tmp_path):
    b, _, clock, evidence, consumer = setup(tmp_path)
    clock[0] += timedelta(minutes=2)
    old = bar(2)
    identity = f'{old.source}:{old.interval_seconds}:{int(old.closed_at.timestamp())}'
    evidence.begin(identity, old, True, old.closed_at)
    consumer.closed_bar(old)
    assert len(evidence.proposals) == 1
    assert identity not in evidence.pending


def test_actual_preview_restart_preserves_identity_and_censoring(tmp_path):
    protocol, paper = tmp_path / 'protocol.db', tmp_path / 'paper.db'
    b, fake = bridge(protocol, paper)
    b.preview = True
    at = [NOW]
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture', clock=lambda: at[0])
    consumer = ShadowConsumer(b, Control(), evidence, clock=lambda: at[0])
    consumer.before_poll()
    at[0] += timedelta(minutes=1)
    consumer.closed_bar(bar(1))
    original = dict(next(iter(evidence.proposals.values())))
    b.gateway.adapter.store.close()
    b.controller.store.close()
    b, fake = bridge(protocol, paper, fake)
    b.preview = True
    restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: at[0])
    consumer = ShadowConsumer(b, Control(), restored, clock=lambda: at[0])
    consumer.before_poll()
    recovered = next(iter(restored.proposals.values()))
    assert recovered['id'] == original['id']
    assert recovered['proposed_at'] == original['proposed_at']
    assert recovered['deadline'] == original['deadline']
    assert recovered['right_censored']
    assert recovered['reason'].startswith('STRATEGY_INVALIDATED')
    assert all(method == 'GET' for method, _, _ in fake.calls)


def test_corrupt_tail_is_not_silently_discarded(tmp_path):
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture')
    with evidence.path.open('ab') as stream:
        stream.write(b'"{partial')
    import csv
    with pytest.raises(csv.Error):
        ShadowEvidence(evidence.path, version='fixture')


def test_file_budget_and_binding(tmp_path):
    _, _, _, evidence, _ = setup(tmp_path, max_bytes=65536)
    with pytest.raises(ObservationFull):
        evidence.append(dict(kind='large', data='x' * 60000))
    evidence.end('FILE_LIMIT')
    assert evidence.path.stat().st_size <= 65536
    assert evidence.summary()['right_censored'] == 1
    with pytest.raises(ValueError, match='binding differs'):
        ShadowEvidence(evidence.path, version='different', max_bytes=65536)


def test_same_response_book_hook_and_zero_writes(tmp_path):
    class Consumer:
        def closed_bar(self, value):
            pass
        def quote(self, value):
            self.quote_value = value
        def observe_book(self, raw, received):
            self.raw, self.received = raw, received
    consumer = Consumer()
    feed, transport, _ = make(tmp_path, consumer=consumer)
    try:
        feed.poll()
        assert D(consumer.raw['bidPrice']) < consumer.quote_value.price < D(consumer.raw['askPrice'])
        age = (consumer.received - consumer.quote_value.observed_at).total_seconds()
        assert 0 < age <= feed.max_quote_age
        assert all(r.method == 'GET' for r in transport.requests)
    finally:
        feed.close()


def test_preview_composition_has_zero_writes_and_separate_journals(network_grid):
    from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
    root, credentials, factory, _, fake, requests = network_grid
    at = [NOW]
    evidence = ShadowEvidence(root / 'evidence.csv', version='fixture', clock=lambda: at[0])
    with open_grid_session(root / 'research', credentials, factory, preview=True,
                           continuous=True) as b:
        consumer = ShadowConsumer(b, Control(), evidence, clock=lambda: at[0])
        consumer.before_poll()
        at[0] += timedelta(minutes=1)
        consumer.closed_bar(bar(1))
        at[0] += timedelta(seconds=1)
        consumer.observe_book(book(at[0], '143', '144'), at[0])
        assert len(evidence.proposals) == 1
        assert b.account().inventory == 0
        assert b.preview
    assert all(request.get_method() == 'GET' for request in requests)
    assert (root / 'research' / 'grid-preview').is_dir()
    assert not (root / 'research' / 'runtime.sqlite3').exists()


def test_disconnect_stops_and_preserves_interruption_reason(tmp_path):
    from types import SimpleNamespace
    _, _, _, evidence, consumer = setup(tmp_path)
    stopped = []
    consumer.supervisor = SimpleNamespace(feed=SimpleNamespace(reason='STALE_OR_FUTURE_QUOTE'),
                                          stop=lambda: stopped.append(True))
    consumer.disconnect()
    assert stopped == [True]
    assert next(r for r in evidence.rows if r['kind'] == 'interruption')['reason'] == 'STALE_OR_FUTURE_QUOTE'
    assert evidence.summary()['right_censored'] == 1


@pytest.mark.parametrize(('host_offset_seconds', 'disconnect'), [
    (-20, False), (20, True), (0, False),
])
def test_preview_lifecycle_uses_session_calibrated_clock(network_grid, monkeypatch,
                                                        host_offset_seconds, disconnect):
    from functools import partial
    from factorlab.integrations.binance_futures_trading import forward_observation as observation
    from factorlab.integrations.binance_futures_trading import network, protocol
    from factorlab.integrations.binance_futures_trading.demo_session import DemoSession

    root, credentials, factory, _, _, requests = network_grid
    exchange_at = NOW
    host_offset = timedelta(seconds=host_offset_seconds)

    def host_clock():
        return exchange_at + host_offset

    monkeypatch.setattr(observation, 'utcnow', host_clock)
    monkeypatch.setattr(observation, 'ShadowEvidence', partial(ShadowEvidence, clock=host_clock))
    monkeypatch.setattr(observation, 'DemoCredentialStore', lambda: credentials)
    monkeypatch.setattr(observation, 'grid_factory', lambda settings: factory)
    monkeypatch.setattr(DemoSession, '_now', lambda _: exchange_at)
    monkeypatch.setattr(protocol, 'build_opener', network.build_opener)
    monkeypatch.setattr(observation.signal, 'signal', lambda *_: None)

    ended_proposal = {}

    def run(supervisor, *, seconds):
        nonlocal exchange_at
        consumer = supervisor.before_poll.__self__
        evidence = consumer.evidence
        if ended_proposal:
            resume = next(row for row in reversed(evidence.rows) if row['kind'] == 'resume')
            assert resume['at'] == host_clock().isoformat()
            assert resume['coverage'] == 'UNOBSERVED_DURING_PROCESS_GAP'
            assert evidence.clock is consumer.clock
            assert evidence.clock() == exchange_at
            consumer._end_inactive()
            assert evidence.proposals == {ended_proposal['id']: ended_proposal}
            return

        supervisor.before_poll()
        event = bar(1)
        exchange_at = event.closed_at
        assert consumer.closed_bar(event)
        proposal, = evidence.proposals.values()
        assert proposal['proposed_at'] == proposal['captured_at'] == exchange_at.isoformat()
        exchange_at += timedelta(seconds=3)
        if disconnect:
            supervisor.feed.pause('STALE_OR_FUTURE_QUOTE')
            supervisor.on_pause()
            interruption, = [row for row in evidence.rows if row['kind'] == 'interruption']
            assert interruption['reason'] == 'STALE_OR_FUTURE_QUOTE'
            assert interruption['at'] == exchange_at.isoformat()
            assert supervisor.stopped.is_set()
            expected_reason = 'DATA_INTERRUPTION'
        else:
            consumer.bridge.grid.cancellations.add(proposal['proposal'])
            consumer._end_inactive()
            expected_reason = 'STRATEGY_INVALIDATED:' + str(consumer.bridge.grid.reason)
        assert proposal['ended_at'] == exchange_at.isoformat()
        assert observation.instant(proposal['ended_at']) >= observation.instant(proposal['captured_at'])
        assert proposal['status'] == 'ENDED'
        assert proposal['reason'] == expected_reason
        assert proposal['right_censored']
        ended_proposal.update(proposal)

    monkeypatch.setattr(observation.DemoSupervisor, 'run', run)
    argv = ['--output', str(root / 'observation'), '--version', 'fixture']
    assert main(argv) == 0
    if host_offset_seconds < 0:
        exchange_at += timedelta(seconds=10)
        assert main(argv) == 0
    assert all(request.get_method() == 'GET' for request in requests)


def test_request_admission_is_durable_and_get_only(tmp_path):
    from factorlab.integrations.binance_futures_trading.forward_observation import BoundedRequests, utcnow
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture')
    deadline = utcnow() + timedelta(minutes=1)
    counts = BoundedRequests(evidence, maximum=1, deadline=deadline)
    with pytest.raises(ValueError, match='GET only'):
        counts.before_request('feed', 'POST', '/fapi/v1/order')
    counts.before_request('session', 'GET', '/fapi/v2/account')
    restored = ShadowEvidence(evidence.path, version='fixture')
    resumed = BoundedRequests(restored, maximum=1, deadline=deadline)
    with pytest.raises(ObservationFull, match='request budget'):
        resumed.before_request('feed', 'GET', '/fapi/v1/ticker/bookTicker')
    expired = BoundedRequests(restored, maximum=2, deadline=utcnow() - timedelta(seconds=1))
    with pytest.raises(ObservationFull, match='deadline'):
        expired.before_request('feed', 'GET', '/fapi/v1/time')
    assert sum(r['kind'] == 'request' for r in restored.rows) == 1


def test_api_interruption_blocks_further_requests_after_restart(tmp_path):
    from factorlab.integrations.binance_futures_trading.forward_observation import BoundedRequests, utcnow
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture')
    deadline = utcnow() + timedelta(minutes=1)
    counts = BoundedRequests(evidence, maximum=10, deadline=deadline)
    counts.before_request('feed', 'GET', '/fapi/v1/time')
    counts.record('feed', 'GET', '/fapi/v1/time', .2, 'RATE_LIMIT', {})
    restored = ShadowEvidence(evidence.path, version='fixture')
    resumed = BoundedRequests(restored, maximum=10, deadline=deadline)
    with pytest.raises(ObservationFull, match='interruption'):
        resumed.before_request('feed', 'GET', '/fapi/v1/time')
    assert resumed.used == 1


@pytest.mark.parametrize('reason', ['STALE_POLL', 'CLOCK_SKEW'])
def test_data_interruption_restart_blocks_requests_and_sampling(network_grid, monkeypatch, reason):
    from types import SimpleNamespace
    from factorlab.integrations.binance_futures_trading import forward_observation as observation

    root, credentials, factory, _, _, transport_requests = network_grid
    output = root / 'observation'
    evidence = ShadowEvidence(output / 'events.csv', version='fixture',
                              max_bytes=5 * 1024 * 1024 - 65536)
    started = observation.utcnow()
    deadline = started + timedelta(seconds=180)
    experiment = dict(kind='experiment', started_at=started.isoformat(),
        deadline=deadline.isoformat(), parameters=dict(seconds=180, sample_seconds=5,
            window_seconds=180, max_bytes=5 * 1024 * 1024, max_proposals=20,
            max_requests=1500, settings=None, version='fixture', symbol='NVDAUSDT'))
    evidence.append(experiment)
    counts = observation.BoundedRequests(evidence, maximum=1500, deadline=deadline)
    counts.before_request('feed', 'GET', '/fapi/v1/time')
    counts.record('feed', 'GET', '/fapi/v1/time', .2, None, {})
    with observation.open_grid_session(root / 'preview-state', credentials, factory,
                                       preview=True, continuous=True) as b:
        consumer = ShadowConsumer(b, Control(), evidence)
        consumer.supervisor = SimpleNamespace(feed=SimpleNamespace(reason=reason), stop=lambda: None)
        consumer.disconnect()
    reserved = counts.used
    sent = len(transport_requests)
    books = evidence.summary()['books']
    monkeypatch.setattr(observation, 'DemoCredentialStore', lambda: credentials)
    monkeypatch.setattr(observation, 'grid_factory', lambda settings: factory)
    assert main(['--output', str(output), '--version', 'fixture']) == 0
    summary = json.loads((output / 'summary.json').read_text())
    restored = ShadowEvidence(evidence.path, version='fixture',
                              max_bytes=5 * 1024 * 1024 - 65536)
    assert summary['experiment'] == experiment
    assert observation.utcnow() < observation.instant(summary['experiment']['deadline'])
    assert summary['reserved_requests'] == reserved
    assert sum(r['kind'] == 'request' for r in restored.rows) == reserved
    assert len(transport_requests) == sent
    assert summary['run_books'] == 0
    assert summary['books'] == books
    assert summary['confirmed_exchange_fills'] == 0
    assert summary['no_live_money'] is True
    assert 'interruption' in summary['reason']
    assert any(r['kind'] == 'interruption' and r['reason'] == reason for r in restored.rows)
    assert all(r['error'] is None for r in restored.rows if r['kind'] == 'request_result')
    assert all(request.get_method() == 'GET' for request in transport_requests)
    assert not (output / 'preview-state' / 'runtime.sqlite3').exists()


def test_raw_unknown_quantity_and_baseline_are_preserved(tmp_path):
    _, _, clock, evidence, consumer = setup(tmp_path)
    raw = book(clock[0])
    del raw['askQty']
    consumer.observe_book(raw, clock[0])
    baseline = evidence.rows[-1]
    assert baseline['raw'] == raw
    assert not baseline['updates'][0]['forward']
    assert baseline['updates'][0]['target_covered_at_top'] is None
    clock[0] += timedelta(seconds=1)
    consumer.observe_book(book(clock[0], '143', '144'), clock[0])
    assert baseline['bid'] == '144'
    assert evidence.rows[-1]['updates'][0]['forward']


def test_cli_has_no_execute_and_duration_is_bounded(tmp_path):
    with pytest.raises(SystemExit):
        main(['--output', str(tmp_path), '--version', 'fixture', '--execute'])
    with pytest.raises(SystemExit):
        main(['--output', str(tmp_path), '--version', 'fixture', '--seconds', '3601'])
    assert not list(tmp_path.iterdir())
