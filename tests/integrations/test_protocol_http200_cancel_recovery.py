"""Deterministic recovery tests for HTTP 200 unconfirmed write receipts."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading import demo_strategy_supervisor as supervisor_module
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_market_feed import DemoMarketFeed
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.demo_session import DemoSession
from factorlab.integrations.binance_futures_trading.demo_strategy_supervisor import StrategyConsumer, StrategyControl
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.protocol import Environment, FuturesClient, OfflineTransport, SafetyBlocked
from factorlab.trading.contracts import OrderStatus as S
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import deployment
from test_protocol_cancel_outbox import (
    cancel_once,
    confirmed,
    economic_state,
    evidence,
    open_runner,
    pending_cancel,
)
from test_protocol_order_outbox import demo_clock, order_writes


def _http200_body(kind, key):
    if kind == 'malformed_json':
        return b'not-valid-json'
    if kind == 'missing_core_fields':
        return json.dumps({'symbol': 'NVDAUSDT', 'cumQuote': '0'}).encode()
    if kind == 'missing_execution_fields':
        return json.dumps({
            'clientOrderId': key,
            'symbol': 'NVDAUSDT',
            'orderId': 1,
            'status': 'CANCELED',
            'cumQuote': '0',
        }).encode()
    raise ValueError(f'unknown body kind: {kind}')


def _http200_post_body(kind, key):
    if kind == 'malformed_json':
        return b'not-valid-json'
    if kind == 'missing_core_fields':
        return json.dumps({'symbol': 'NVDAUSDT', 'cumQuote': '0'}).encode()
    if kind == 'missing_execution_fields':
        return json.dumps({
            'clientOrderId': key,
            'symbol': 'NVDAUSDT',
            'orderId': 1,
            'status': 'NEW',
            'cumQuote': '0',
        }).encode()
    raise ValueError(f'unknown post body kind: {kind}')


@pytest.mark.parametrize('body_kind', ['malformed_json', 'missing_core_fields', 'missing_execution_fields'])
def test_http200_unconfirmed_delete_recovers_in_process_and_retries(
        deployment, demo_clock, cancel_once, monkeypatch, body_kind, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        original = NetworkFixture.__call__
        injected = False
        with monkeypatch.context() as patch:
            def corrupt_delete(self, request, timeout):
                nonlocal injected
                if request.method == 'DELETE' and not injected:
                    injected = True
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(200, {}, _http200_body(body_kind, key))
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', corrupt_delete)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert pending_cancel(runner, key)
        assert counts['POST'] == 1
        assert counts['DELETE'] == 1
        with closing(DemoRuntime(deployment[0])) as audit:
            assert audit.active_campaign()['id'] == deployment[2]['id']
            persisted = [r for r in audit.receipts(deployment[2]['id']) if r['method'] == 'DELETE']
            assert len(persisted) == 1
            assert persisted[0]['http_status'] == 200
            assert persisted[0]['outcome_unknown'] is True
        # Same-process GET recovery confirms the original order is still active.
        demo_clock.at += timedelta(seconds=1)
        assert runner.recover().reconciled
        assert not session.write_uncertain
        assert session.adapter.order(key).status is S.SUBMITTED
        assert pending_cancel(runner, key)
        # Retry DELETE in the same process without restarting or refreshing capability.
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
        assert runner.quote(demo_clock.quote()) is None
    assert counts['POST'] == 1
    assert counts['DELETE'] == 2
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key), ('DELETE', key)]
    with closing(DemoRuntime(deployment[0])) as audit:
        history = list(audit.db.execute('SELECT client_id, receipt FROM dispatch_retry_history'))
        assert len(history) == 1
        archived = json.loads(history[0][1])
        assert history[0][0] == key
        assert archived['http_status'] == 200
        assert archived['outcome_unknown'] is True
        receipts = [r for r in audit.receipts(deployment[2]['id']) if r['method'] == 'DELETE']
        assert len(receipts) == 1 and len(receipts[0]['previous_attempts']) == 1
        assert audit.post_count(deployment[2]['id']) == 1
    evidence(record_property, counts)


def test_http200_unconfirmed_delete_when_get_confirms_canceled_does_not_repeat_delete(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def apply_but_corrupt(self, request, timeout):
                if request.method == 'DELETE':
                    original(self, request, timeout)
                    return Response(200, {}, b'{malformed')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', apply_but_corrupt)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert pending_cancel(runner, key)
        assert counts['DELETE'] == 1
        demo_clock.at += timedelta(seconds=1)
        assert runner.recover().reconciled
        assert not session.write_uncertain
        confirmed(runner, key, S.CANCELED)
        assert runner.quote(demo_clock.quote()) is None
    assert counts['POST'] == 1
    assert counts['DELETE'] == 1
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    with closing(DemoRuntime(deployment[0])) as audit:
        assert list(audit.db.execute('SELECT client_id FROM dispatch_retry_history')) == []
        assert audit.post_count(deployment[2]['id']) == 1
    evidence(record_property, counts)


def test_http200_unconfirmed_delete_on_partial_fill_cancels_remainder_and_preserves_fills(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        exchange.trade_update(key, '.01', '99')
        assert runner.recover().reconciled
        before = session.adapter.order(key)
        economics = economic_state(runner)
        original = NetworkFixture.__call__
        injected = False
        with monkeypatch.context() as patch:
            def corrupt_once(self, request, timeout):
                nonlocal injected
                if request.method == 'DELETE' and not injected:
                    injected = True
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(200, {}, b'not-json')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', corrupt_once)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert counts['DELETE'] == 1
        demo_clock.at += timedelta(seconds=1)
        assert runner.recover().reconciled
        assert not session.write_uncertain
        assert session.adapter.order(key).status is S.PARTIALLY_FILLED
        assert session.adapter.order(key).cumulative_filled_quantity == D('.01')
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key, S.CANCELED)
        after = session.adapter.order(key)
        assert after.cumulative_filled_quantity == before.cumulative_filled_quantity == D('.01')
        assert after.cumulative_fee == before.cumulative_fee
        assert economic_state(runner) == economics
    assert counts['POST'] == 1
    assert counts['DELETE'] == 2
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_http200_unconfirmed_delete_failed_get_recovery_never_retries_blindly(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def corrupt_delete(self, request, timeout):
                if request.method == 'DELETE':
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(200, {}, b'not-json')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', corrupt_delete)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert counts['DELETE'] == 1
        with monkeypatch.context() as patch:
            patch.setattr(session.gateway, 'recover', lambda *a, **kw: (_ for _ in ()).throw(OSError('GET unavailable')))
            with pytest.raises(OSError, match='GET unavailable'):
                runner.recover()
        assert session.write_uncertain
        assert pending_cancel(runner, key)
        demo_clock.at += timedelta(seconds=1)
        assert runner.quote(demo_clock.quote()) is None
        with pytest.raises(SafetyBlocked, match='known active order|GET-only recovery'):
            session.cancel(key)
        assert counts['DELETE'] == 1
        # Once GET succeeds, the same process can complete cancellation.
        assert runner.recover().reconciled
        assert not session.write_uncertain
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 2
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_http200_valid_receipt_keeps_original_behavior(
        deployment, demo_clock, cancel_once, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        assert not session.write_uncertain
        demo_clock.at += timedelta(seconds=1)
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        assert not session.write_uncertain
        confirmed(runner, key)
        assert all(not r['outcome_unknown'] and r['http_status'] == 200 for r in session.receipts)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 1
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    with closing(DemoRuntime(deployment[0])) as audit:
        assert list(audit.db.execute('SELECT client_id FROM dispatch_retry_history')) == []
        assert audit.active_campaign()['id'] == deployment[2]['id']
    evidence(record_property, counts)


@pytest.mark.parametrize('body_kind', ['malformed_json', 'missing_core_fields', 'missing_execution_fields'])
def test_http200_uncertain_post_stays_blocked_after_get_recovery(
        deployment, demo_clock, cancel_once, monkeypatch, body_kind, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.journal.deployment_id + '-entry'
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def corrupt_post(self, request, timeout):
                if request.method == 'POST':
                    original(self, request, timeout)
                    return Response(200, {}, _http200_post_body(body_kind, key))
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', corrupt_post)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert counts['POST'] == 1
        assert counts['DELETE'] == 0
        with closing(DemoRuntime(deployment[0])) as audit:
            # Non-continuous campaign must be stopped on unconfirmed HTTP 200 POST.
            assert audit.active_campaign() is None
            post_receipts = [r for r in audit.receipts(deployment[2]['id']) if r['method'] == 'POST']
            assert len(post_receipts) == 1
            assert post_receipts[0]['http_status'] == 200
            assert post_receipts[0]['outcome_unknown'] is True
        demo_clock.at += timedelta(seconds=1)
        recovered = session.recover()
        assert recovered['reconciled']
        assert recovered['unknown'] == []
        assert session.adapter.order(key).status is S.SUBMITTED
        # POST uncertainty must NOT be cleared by DELETE recovery logic.
        assert session.write_uncertain
        with pytest.raises(SafetyBlocked, match='GET-only recovery'):
            session.cancel(key)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 0
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key)]
    evidence(record_property, counts)


@pytest.mark.parametrize('delete_status', [200, 500])
def test_mixed_uncertain_post_and_delete_never_clears_write_uncertain(
        deployment, demo_clock, cancel_once, monkeypatch, delete_status, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        # Inject an unresolved HTTP 200 POST receipt in the same unrecovered receipt window.
        session.receipts[0]['outcome_unknown'] = True
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def fail_delete(self, request, timeout):
                if request.method == 'DELETE':
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(delete_status, {}, b'not-json')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', fail_delete)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        demo_clock.at += timedelta(seconds=1)
        recovered = session.recover()
        assert recovered['reconciled']
        assert recovered['unknown'] == []
        # Mixed POST + DELETE uncertainty must remain closed.
        assert session.write_uncertain
        with pytest.raises(SafetyBlocked, match='GET-only recovery'):
            session.cancel(key)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 1
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_http400_unknown_delete_preserves_http400_stop_guard(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def http400_unknown(self, request, timeout):
                if request.method == 'DELETE':
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(400, {}, b'{"code":-1006,"msg":"unexpected"}')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', http400_unknown)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        with closing(DemoRuntime(deployment[0])) as audit:
            assert audit.active_campaign() is None
        demo_clock.at += timedelta(seconds=1)
        assert session.recover()['reconciled']
        assert session.write_uncertain
        with pytest.raises(SafetyBlocked, match='GET-only recovery|HTTP 400'):
            session.cancel(key)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 1
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_supervisor_recovers_http200_uncertain_cancel_without_process_restart(
        deployment, cancel_once, monkeypatch, capsys, record_property):
    root, store, campaign, exchange = deployment
    at = [NOW + timedelta(milliseconds=100)]
    boundary = int(NOW.timestamp()) // 60 * 60000

    class StepMarket(OfflineTransport):
        def clock(self):
            return at[0].timestamp() + .005

        def client(self):
            return FuturesClient(environment=Environment.DEMO, transport=self,
                                 clock=self.clock, monotonic=self.clock)

        def __call__(self, request, timeout):
            ms = round(at[0].timestamp() * 1000)
            if request.path.endswith('/time'):
                payload = dict(serverTime=ms + 5)
            elif request.path.endswith('/klines'):
                payload = [[boundary - 60000, '100', '100', '100', '100', '100', boundary - 1]]
            else:
                assert request.path.endswith('/bookTicker')
                payload = dict(symbol='NVDAUSDT', bidPrice='100', askPrice='100', time=ms)
            return Response(200, {}, json.dumps(payload).encode())

    market = StepMarket()
    monkeypatch.setattr(DemoSession, '_now', lambda session: at[0])
    original = NetworkFixture.__call__
    delete_corrupted = False

    def exchange_io(self, request, timeout):
        nonlocal delete_corrupted
        if request.path.endswith('/bookTicker'):
            return market(request, timeout)
        if request.method == 'DELETE' and not delete_corrupted:
            delete_corrupted = True
            self.calls.append(('DELETE', request.path, parse_qs(request.query)))
            return Response(200, {}, b'not-valid-json')
        return original(self, request, timeout)

    monkeypatch.setattr(NetworkFixture, '__call__', exchange_io)
    monkeypatch.setattr(supervisor_module, 'MarketClient', lambda requests: market.client())
    monkeypatch.setattr(supervisor_module, 'DemoMarketFeed', lambda *a, **kw: DemoMarketFeed(
        *a, clock=lambda: at[0], **kw))

    supervisor_type = supervisor_module.DemoSupervisor

    class ThreePollSupervisor(supervisor_type):
        def run(self, *, seconds=None):
            report = self.report
            polls = [0]

            def on_report(raw):
                report(raw)
                polls[0] += 1
                at[0] += timedelta(seconds=1)
                if polls[0] >= 3:
                    self.stop()

            self.report = on_report
            super().run(seconds=seconds)

    monkeypatch.setattr(supervisor_module, 'DemoSupervisor', ThreePollSupervisor)
    counts = dict(GET=0, POST=0, DELETE=0)
    args = SimpleNamespace(execute=True, interval='1m', strategy=str(cancel_once.manifest_path),
                           max_backfill=120, poll_seconds=.001, seconds=60)
    with closing(DemoRuntime(root)) as runtime:
        supervisor_module.run_worker(args, root, store, runtime, counts)
        assert runtime.post_count(campaign['id']) == 1
        history = list(runtime.db.execute('SELECT client_id, receipt FROM dispatch_retry_history'))
        assert len(history) == 1
        assert json.loads(history[0][1])['http_status'] == 200
        assert json.loads(history[0][1])['outcome_unknown'] is True

    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    key = campaign['prefix'] + '-entry'
    assert len(reports) == 3
    # Poll 1: POST accepted; Poll 2: DELETE returns HTTP 200 malformed JSON;
    # Poll 3: Same-process GET recovery clears write_uncertain and finishes DELETE.
    assert reports[0]['account']['orders'][key]['status'] == 'SUBMITTED'
    assert reports[1]['account']['orders'][key]['status'] == 'UNKNOWN'
    assert reports[2]['account']['orders'][key]['status'] == 'CANCELED'
    assert reports[2]['reason'] is None
    assert reports[2]['strategy']['pending'] is None
    assert counts['POST'] == 1
    assert counts['DELETE'] == 2
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_http200_uncertain_delete_sqlite_restart_and_legacy_receipt_compat(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def corrupt_delete(self, request, timeout):
                if request.method == 'DELETE':
                    self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    return Response(200, {}, b'not-json')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', corrupt_delete)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        offer = pending_cancel(runner, key)
    # Verify legacy receipt compatibility (without outcome_unknown field) for confirmed ACK.
    with closing(DemoRuntime(deployment[0])) as audit:
        post_raw = audit.db.execute("SELECT receipt FROM dispatches WHERE method='POST'").fetchone()[0]
        legacy = json.loads(post_raw)
        legacy.pop('outcome_unknown', None)
        audit.record_receipt(deployment[2], legacy)
        assert audit.active_campaign()['id'] == deployment[2]['id']
    # Reopen from SQLite and finish cancellation after newer GET recovery.
    demo_clock.at += timedelta(seconds=1)
    with open_runner(deployment, cancel_once, counts, recover=False) as (session, runner):
        assert pending_cancel(runner, key) == offer
        assert runner.recover().reconciled
        assert not session.write_uncertain
        assert session.adapter.order(key).status is S.SUBMITTED
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
    assert counts['POST'] == 1
    assert counts['DELETE'] == 2
    assert counts['GET'] > 0
    assert order_writes(exchange) == [('POST', key), ('DELETE', key), ('DELETE', key)]
    with closing(DemoRuntime(deployment[0])) as audit:
        history = list(audit.db.execute('SELECT client_id, receipt FROM dispatch_retry_history'))
        assert len(history) == 1
        assert history[0][0] == key and json.loads(history[0][1])['outcome_unknown'] is True
    evidence(record_property, counts)


def test_continuous_record_receipt_and_report_reject_http200_unconfirmed(deployment):
    root, store, _, _ = deployment
    binding = store.read_config()
    with closing(DemoRuntime(root)) as runtime:
        runtime.stop()
        auth = runtime.start_continuous(binding, opening_equity=D('1000'), stop_epoch=runtime.stop_epoch())
        campaign = dict(id='window-test', authorization=auth['id'], journal_id=auth['journal_id'],
                        prefix=auth['prefix'], expires_at='2099-01-01T00:00:00+00:00', seconds=3600,
                        orders=4, opening_equity='1000', max_loss='1', smoke=False,
                        policy=auth['policy'], account_configuration=binding['configuration'])
        with runtime.db:
            runtime.db.execute('INSERT INTO campaigns VALUES (?, ?)', (campaign['id'], json.dumps(campaign)))
            runtime.db.execute('INSERT INTO dispatches VALUES (?, ?, ?, ?, NULL)',
                               (campaign['id'], 'POST', auth['prefix'] + '-01', json.dumps({'quantity': '0.04', 'price': '100'})))
            runtime._set('continuous_window', campaign['id'])
        bad = dict(method='POST', http_status=200, client_order_id=auth['prefix'] + '-01',
                   outcome_unknown=True, code=None, order_id=None, status=None, filled_quantity=None)
        runtime.record_receipt(campaign, bad)
        assert runtime._get('continuous_recovery')
        report = runtime.report('smoke-v1')
        assert report['ACCOUNT_EXECUTION_STATE']['recovery_required'] is True
        assert report['EXCHANGE_WRITE_CAPABILITY']['historical_success'] is False

