"""HTTP-intercepted equivalence of single-round Shadow recovery."""
from copy import deepcopy
from datetime import timedelta
import json
from time import perf_counter

import pytest

from factorlab.integrations.binance_futures_trading.demo_grid_supervisor import GridConsumer
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.forward_observation import ShadowConsumer, ShadowEvidence, instant
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from test_binance_demo_grid_network import network_grid, wired
from test_demo_grid_supervisor import Control, bar
from test_binance_demo_grid_bridge import bridge as offline_bridge, observe
from test_forward_observation import book


class OriginalShadowConsumer(ShadowConsumer):
    def recovered(self):
        pass

    def before_poll(self):
        if self.evidence.pending:
            self.bridge.recover()
            self.evidence.recover(self.bridge)
        GridConsumer.before_poll(self)
        self._end_inactive()
        self.evidence.end('WINDOW_END', ids=[k for k, p in self.evidence.proposals.items()
            if self.clock() >= instant(p['deadline'])])


def semantics(bridge, consumer, evidence):
    return dict(account=deepcopy(bridge.account()), grid=deepcopy(bridge.grid.deployment()),
        proposals=deepcopy(evidence.proposals), pending=deepcopy(evidence.pending),
        running=consumer.running, live_bar=consumer.live_bar, reason=consumer.reason)


@pytest.mark.parametrize('pending', [False, True])
def test_same_authoritative_recovery_and_proposals_with_fewer_gets(network_grid, tmp_path, monkeypatch, pending):
    from test_binance_demo_network import NetworkFixture
    root, credentials, factory, _, fake, requests = network_grid
    virtual = [0.]
    dispatch = NetworkFixture.__call__
    def timed_dispatch(self, request, timeout):
        virtual[0] += .25
        return dispatch(self, request, timeout)
    monkeypatch.setattr(NetworkFixture, '__call__', timed_dispatch)
    reports, results = {}, {}
    for name, cls in [('original', OriginalShadowConsumer), ('shared', ShadowConsumer)]:
        with open_grid_session(root / name, credentials, factory, preview=True, continuous=True) as bridge:
            at = [NOW]
            evidence = ShadowEvidence(root / name / 'events.csv', version='fixture', clock=lambda: at[0])
            consumer = cls(bridge, Control(), evidence, clock=lambda: at[0])
            consumer.before_poll()
            at[0] += timedelta(minutes=1)
            consumer.closed_bar(bar(1))
            if pending:
                capture = next(row for row in evidence.rows if row['kind'] == 'capture')
                evidence._apply(dict(kind='begin', source=capture['source'], at=at[0].isoformat(),
                    market_at=at[0].isoformat(), live=True))
            start, started, simulated_start = len(requests), perf_counter(), virtual[0]
            consumer.before_poll()
            elapsed = perf_counter() - started
            count = len(requests) - start
            reports[name] = dict(gets=count, wall_seconds=elapsed,
                deterministic_http_seconds=virtual[0] - simulated_start, http_seconds_per_request=.25)
            # Deliver the same actual feed responses, including durable cursor acknowledgements.
            from factorlab.integrations.binance.client import Response
            from test_demo_market_feed import Transport, make
            class MarketTransport(Transport):
                def __call__(self, request, timeout):
                    virtual[0] += .25
                    if request.path.endswith('/bookTicker'):
                        self.requests.append(request)
                        assert request.method == 'GET'
                        return Response(200, {}, json.dumps(book(at[0], '149', '151')).encode())
                    response = super().__call__(request, timeout)
                    if request.path.endswith('/klines'):
                        rows = json.loads(response.body)
                        for row in rows:
                            row[1:6] = ['150', '153', '147', '150', '10']
                        return Response(200, {}, json.dumps(rows).encode())
                    return response
            transport = MarketTransport()
            monkeypatch.setattr(bridge.gateway, 'clock', lambda: at[0])
            feed, _, _ = make(root / name, transport, consumer)
            try:
                for second in (1, 6, 11):
                    at[0] = NOW + timedelta(minutes=1, seconds=second)
                    transport.time = int(at[0].timestamp() * 1000)
                    assert feed.poll()['quotes'] == 1
                assert len(transport.requests) == 9
                reports[name]['first_round_gets_including_feed'] = count + 3
            finally:
                feed.close()
            assert evidence.summary()['books'] == 3
            assert not evidence.pending
            result = semantics(bridge, consumer, evidence)
            # Source IDs and deployment IDs are independently allocated per preview.
            result['grid'].pop('deployment_id', None)
            result['proposals'] = [{k: v for k, v in p.items() if k not in
                {'id', 'deployment', 'proposal', 'deployment_config', 'configuration_hash'}}
                for p in result['proposals'].values()]
            results[name] = result
    assert results['original'] == results['shared']
    assert reports['shared']['gets'] == 13
    assert reports['original']['gets'] == (26 if pending else 13)
    assert all(r.get_method() == 'GET' for r in requests)
    print('RECOVERY_COMPARISON=' + json.dumps(dict(pending=pending, **reports), sort_keys=True))


def test_capture_recovered_before_grid_withdrawal(network_grid, monkeypatch):
    root, credentials, factory, _, fake, requests = network_grid
    with open_grid_session(root, credentials, factory, preview=True, continuous=True) as bridge:
        at = [NOW]
        evidence = ShadowEvidence(root / 'events.csv', version='fixture', clock=lambda: at[0])
        consumer = ShadowConsumer(bridge, Control(), evidence, clock=lambda: at[0])
        consumer.before_poll()
        at[0] += timedelta(minutes=1)
        original_capture = evidence.capture
        def crash(*args, **kwargs):
            raise OSError('interrupted capture')
        monkeypatch.setattr(evidence, 'capture', crash)
        with pytest.raises(OSError, match='interrupted capture'):
            consumer.closed_bar(bar(1))
        monkeypatch.setattr(evidence, 'capture', original_capture)
        assert evidence.pending
        # A new process must capture persisted source proposals before startup withdrawal.
        restored = ShadowEvidence(evidence.path, version='fixture', clock=lambda: at[0])
        resumed = ShadowConsumer(bridge, Control(), restored, clock=lambda: at[0])
        resumed.before_poll()
        assert not restored.pending
        assert len(restored.proposals) == 1
        proposal = next(iter(restored.proposals.values()))
        assert proposal['status'] == 'ENDED'
        assert proposal['reason'].startswith('STRATEGY_INVALIDATED:')
        assert all(r.get_method() == 'GET' for r in requests)


@pytest.mark.parametrize('state', ['UNKNOWN', 'FILLED'])
@pytest.mark.parametrize('cls', [OriginalShadowConsumer, ShadowConsumer])
def test_pending_recovery_preserves_unknown_and_completed_order_constraints(tmp_path, state, cls):
    from decimal import Decimal
    from factorlab.trading import OrderStatus
    bridge, fake = offline_bridge()
    if state == 'UNKNOWN':
        fake.timeout_next = True
        assert observe(bridge) is None
        assert any(record.snapshot.reconcile_required for record in bridge.gateway.state.orders.values())
    else:
        assert observe(bridge).status is OrderStatus.SUBMITTED
        key = bridge.grid.active[0].intent.client_order_id
        fake.trade_update(key, '.04', '145')
    writes = sum(method != 'GET' for method, _, _ in fake.calls)
    bridge.preview = True
    evidence = ShadowEvidence(tmp_path / 'events.csv', version='fixture', clock=lambda: NOW)
    evidence.pending['missing-source'] = dict(kind='begin')
    consumer = cls(bridge, Control(), evidence, clock=lambda: NOW)
    consumer.before_poll()
    assert bridge.ready
    assert not any(record.snapshot.reconcile_required for record in bridge.gateway.state.orders.values())
    assert bridge.account().inventory == (Decimal('.04') if state == 'FILLED' else 0)
    assert sum(method != 'GET' for method, _, _ in fake.calls) == writes
    turnover = bridge.account().turnover
    consumer.before_poll()
    assert bridge.account().turnover == turnover
    bridge.gateway.adapter.store.close()
    bridge.controller.store.close()


@pytest.mark.parametrize('error', [SafetyBlocked('ACCOUNT_NOT_RECONCILED'), OSError('timeout')])
def test_failed_account_recovery_never_replays_or_decides(network_grid, monkeypatch, error):
    root, credentials, factory, _, fake, requests = network_grid
    with open_grid_session(root, credentials, factory, preview=True, continuous=True) as bridge:
        evidence = ShadowEvidence(root / 'events.csv', version='fixture')
        evidence.pending['uncommitted'] = dict(kind='begin')
        consumer = ShadowConsumer(bridge, Control(), evidence)
        def fail():
            bridge.ready = False
            raise error
        monkeypatch.setattr(bridge, 'recover', fail)
        def replay(_):
            pytest.fail('proposal recovery must not precede authoritative account recovery')
        monkeypatch.setattr(evidence, 'recover', replay)
        with pytest.raises((FeedPaused, OSError)):
            consumer.before_poll()
        assert not consumer.running and not bridge.ready
        assert evidence.pending
