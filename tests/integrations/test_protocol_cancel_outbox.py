"""One-shot cancellations survive failures until the controller confirms an outcome."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
import json
from urllib.parse import parse_qs

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.unified_strategy import strategy_runtime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.contracts import OrderStatus as S
from factorlab.trading.realtime import identity
from factorlab.trading.storage import PaperStore, decode
from test_binance_demo_network import NetworkFixture, wired
from test_binance_demo_strategy_session import deployment
from test_protocol_order_outbox import demo_clock, order_writes
from test_protocol_research_outstanding import paper_controller
from test_unified_strategy import manifest


@pytest.fixture
def cancel_once(tmp_path):
    return manifest(tmp_path / 'cancel_once', '''
from decimal import Decimal as D
from factorlab.strategies.protocol import Decision, StrategySpec
from factorlab.trading.contracts import OrderIntent, OrderType, Side

class CancelOnce:
    def describe(self):
        return StrategySpec('cancel_once', '1', 'orders', ['NVDAUSDT'], [60], ['quote'])

    def decide(self, event, state):
        key = event['deployment_id'] + '-entry'
        if not state:
            order = OrderIntent(event['account']['market'], 'NVDAUSDT', Side.BUY,
                OrderType.LIMIT, D('.04'), D(99), key, position_side='BOTH')
            return Decision(orders=(order,), state={'opened': True})
        if not state.get('canceled'):
            return Decision(cancellations=(key,), state={'opened': True, 'canceled': True})
        return Decision(state=state)
''', 'CancelOnce')


@contextmanager
def open_runner(deployment, strategy, counts, *, recover=True, authorized=True):
    root, credentials, campaign, _ = deployment
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, credentials, runtime, counts,
                          campaign=campaign if authorized else None) as (session, expect):
            runner = strategy_runtime(session, strategy=strategy, interval_seconds=60,
                deployment_id=campaign['prefix'] + '-cancel',
                client_id_prefix=campaign['prefix'] + '-', expect=expect)
            if recover:
                assert runner.recover().reconciled
            yield session, runner


def pending_cancel(runner, key):
    pending = runner.gateway.state.runtime['pending']
    assert [offer['key'] for offer in pending['cancellations']] == [key]
    return pending['cancellations'][0]


def attempt_id(runner, offer):
    return runner.execution.prefix + 'cancel:' + identity((offer['key'], offer['eligible_at']))


def confirmed(runner, key, status=S.CANCELED):
    assert runner.execution.cancellation_confirmed(key)
    assert runner.gateway.state.orders[key].snapshot.status is status
    assert runner.gateway.state.runtime['pending'] is None
    outcomes = [decode(json.loads(raw)) for name, raw in runner.controller.store.db.execute(
        'SELECT id, payload FROM events') if ':cancel:' in name and name.endswith(':outcome')]
    assert len(outcomes) == 1
    assert outcomes[0]['client_order_id'] == key and outcomes[0]['status'] is status


def evidence(record_property, counts):
    record_property('requests', json.dumps(counts, sort_keys=True))


def economic_state(runner):
    return (deepcopy(runner.gateway.state.cash),
            {key: (p.quantity, p.entry_price, p.quote_currency)
             for key, p in runner.gateway.state.positions.items()})


@pytest.mark.parametrize('partial', [False, True])
def test_one_shot_cancel_confirms_once_and_preserves_fills(
        deployment, demo_clock, cancel_once, partial, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        if partial:
            exchange.trade_update(key, '.01', '99')
            assert runner.recover().reconciled
        before = session.adapter.order(key)
        economics = economic_state(runner)
        demo_clock.at += timedelta(seconds=1)
        quote = demo_clock.quote()
        assert runner.quote(quote).status is S.CANCELED
        confirmed(runner, key)
        assert session.adapter.order(key).cumulative_filled_quantity == before.cumulative_filled_quantity
        assert session.adapter.order(key).cumulative_fee == before.cumulative_fee
        assert economic_state(runner) == economics
        assert runner.quote(quote) is None
    with open_runner(deployment, cancel_once, counts) as (_, runner):
        confirmed(runner, key)
        assert runner.quote(quote) is None
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


@pytest.mark.parametrize('legacy_attempt', [False, True])
def test_crash_after_attempt_before_cancel_restarts_with_original_identity(
        deployment, demo_clock, cancel_once, monkeypatch, legacy_attempt, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        demo_clock.at += timedelta(seconds=1)
        if legacy_attempt:
            def register_legacy(decision, **kwargs):
                marker = kwargs['cancellation_attempts'][key]
                with runner.controller.store.transaction():
                    runner.controller.store.record_metadata(marker, dict(client_order_id=key, at=demo_clock.at))
                raise RuntimeError('crash before cancel')
            monkeypatch.setattr(runner.execution, 'dispatch', register_legacy)
        else:
            monkeypatch.setattr(session, 'cancel', lambda _: (_ for _ in ()).throw(RuntimeError('crash before cancel')))
        with pytest.raises(RuntimeError, match='crash before cancel'):
            runner.quote(demo_clock.quote())
        offer = pending_cancel(runner, key)
        assert runner.controller.store.event_payload(attempt_id(runner, offer)) is not None
        assert counts['DELETE'] == 0
    recovery_start = len(exchange.calls)
    with open_runner(deployment, cancel_once, counts, recover=False) as (_, runner):
        assert pending_cancel(runner, key) == offer
        assert runner.recover().reconciled
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
    recovered = exchange.calls[recovery_start:]
    get_order = next(i for i, (method, path, params) in enumerate(recovered)
        if method == 'GET' and path == '/fapi/v1/order' and params['origClientOrderId'] == [key])
    assert get_order < next(i for i, (method, _, _) in enumerate(recovered) if method == 'DELETE')
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_pre_delete_facts_failure_retries_after_get_past_entry_deadline(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        demo_clock.at += timedelta(seconds=1)
        with monkeypatch.context() as patch:
            def fail():
                raise OSError('bound facts unavailable before DELETE')
            patch.setattr(session, '_fresh_bound_facts', fail)
            with pytest.raises(OSError, match='bound facts unavailable'):
                runner.quote(demo_clock.quote())
        pending_cancel(runner, key)
        assert counts['DELETE'] == 0
        recovery_start = len(exchange.calls)
        demo_clock.at += timedelta(seconds=61)
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
    recovered = exchange.calls[recovery_start:]
    assert any(method == 'GET' and path == '/fapi/v1/order' and params['origClientOrderId'] == [key]
               for method, path, params in recovered)
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


@pytest.mark.parametrize(('apply_delete', 'partial'), [(True, False), (True, True), (False, False), (False, True)])
@pytest.mark.parametrize('restart', [False, True])
def test_lost_delete_reply_reconciles_terminal_or_retries_confirmed_active(
        deployment, demo_clock, cancel_once, monkeypatch, apply_delete, partial, restart, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        if partial:
            exchange.trade_update(key, '.01', '99')
            assert runner.recover().reconciled
        before = session.adapter.order(key)
        economics = economic_state(runner)
        original = NetworkFixture.__call__
        with monkeypatch.context() as patch:
            def lose_reply(self, request, timeout):
                if request.method == 'DELETE':
                    if apply_delete:
                        original(self, request, timeout)
                    else:
                        self.calls.append(('DELETE', request.path, parse_qs(request.query)))
                    raise TimeoutError('DELETE outcome unknown')
                return original(self, request, timeout)
            patch.setattr(NetworkFixture, '__call__', lose_reply)
            demo_clock.at += timedelta(seconds=1)
            assert runner.quote(demo_clock.quote()).status is S.UNKNOWN
        assert session.write_uncertain
        assert pending_cancel(runner, key)
        assert counts['DELETE'] == 1
        with closing(DemoRuntime(deployment[0])) as audit:
            # Neither an old GET nor a preflight read can authorize replay.
            with pytest.raises(SafetyBlocked, match='already consumed'):
                audit.reserve(deployment[2], 'DELETE', dict(symbol='NVDAUSDT', origClientOrderId=key),
                    session.client.transport.authorization.plan, expected=None, facts=session.adapter.facts,
                    cancel_recovered_at=session.last_reconciled_at)
        with monkeypatch.context() as patch:
            def failed_get(*args, **kwargs):
                raise OSError('GET recovery unavailable')
            patch.setattr(session.gateway, 'recover', failed_get)
            with pytest.raises(OSError, match='GET recovery unavailable'):
                runner.recover()
        pending_cancel(runner, key)
        assert counts['DELETE'] == 1
        demo_clock.at += timedelta(seconds=1)
        assert runner.quote(demo_clock.quote()) is None
        assert counts['DELETE'] == 1
        if not restart:
            assert runner.recover().reconciled
            assert not session.write_uncertain
            if not apply_delete:
                assert runner.quote(demo_clock.quote()).status is S.CANCELED
            confirmed(runner, key)
    recovery_start = len(exchange.calls)
    with open_runner(deployment, cancel_once, counts, recover=False) as (session, runner):
        if restart:
            pending_cancel(runner, key)
        else:
            confirmed(runner, key)
        assert runner.recover().reconciled
        if apply_delete or not restart:
            confirmed(runner, key)
            assert runner.quote(demo_clock.quote()) is None
        else:
            assert session.adapter.order(key).status is (S.PARTIALLY_FILLED if partial else S.SUBMITTED)
            pending_cancel(runner, key)
            assert runner.quote(demo_clock.quote()).status is S.CANCELED
            confirmed(runner, key)
        after = session.adapter.order(key)
        assert (after.cumulative_filled_quantity, after.cumulative_fee) == (
            before.cumulative_filled_quantity, before.cumulative_fee)
        assert economic_state(runner) == economics
    recovered = exchange.calls[recovery_start:]
    assert any(method == 'GET' and path == '/fapi/v1/order' and params['origClientOrderId'] == [key]
               for method, path, params in recovered)
    assert order_writes(exchange) == [('POST', key)] + [('DELETE', key)] * (1 if apply_delete else 2)
    with closing(DemoRuntime(deployment[0])) as audit:
        history = list(audit.db.execute('SELECT client_id, receipt FROM dispatch_retry_history'))
        assert len(history) == (0 if apply_delete else 1)
        if history:
            assert history[0][0] == key and json.loads(history[0][1])['outcome_unknown']
        assert audit.post_count(deployment[2]['id']) == 1
    evidence(record_property, counts)


def test_cancel_survives_pause_disconnect_expiry_restart_without_reviving_entry(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    from dataclasses import replace
    from factorlab.strategies.protocol import Decision
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        opened = runner.quote(demo_clock.quote())
        key = opened.intent.client_order_id
        # An entry waiting behind the active order must stay withdrawn on resume.
        queued = replace(opened.intent, client_order_id=runner.journal.deployment_id + '-queued')
        runner.controller._atomic(lambda: runner._pending(Decision(orders=(queued,)), demo_clock.at, True))
        demo_clock.at += timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(session, '_fresh_bound_facts', lambda: (_ for _ in ()).throw(OSError('offline')))
            with pytest.raises(OSError, match='offline'):
                runner.quote(demo_clock.quote())
        offer = pending_cancel(runner, key)
        runner.pause()
        runner.disconnect()
        pending_cancel(runner, key)
        assert runner.gateway.state.runtime['pending']['orders'] == []
        assert runner.execution.retired(queued.client_order_id, queued)
        demo_clock.at += timedelta(seconds=120)
    with open_runner(deployment, cancel_once, counts, recover=False) as (_, runner):
        assert pending_cancel(runner, key) == offer
        assert runner.gateway.state.runtime['status'] == 'PAUSED'
        assert runner.resume().reconciled
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)
        assert runner.quote(demo_clock.quote()) is None
    assert order_writes(exchange) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


@pytest.mark.parametrize('terminal', ['CANCELED', 'FILLED', 'EXPIRED'])
def test_get_terminal_after_pre_delete_failure_finishes_without_delete(
        deployment, demo_clock, cancel_once, monkeypatch, terminal, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    exchange = deployment[3]
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        demo_clock.at += timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(session, '_fresh_bound_facts', lambda: (_ for _ in ()).throw(OSError('offline')))
            with pytest.raises(OSError):
                runner.quote(demo_clock.quote())
        pending_cancel(runner, key)
        if terminal == 'FILLED':
            exchange.trade_update(key, '.04', '99')
        else:
            exchange.orders[key]['status'] = terminal
        assert runner.quote(demo_clock.quote()) is None
        confirmed(runner, key, S.FILLED if terminal == 'FILLED' else S.CANCELED)
    assert order_writes(exchange) == [('POST', key)]
    evidence(record_property, counts)


def test_retry_keeps_demo_authorization_guard(deployment, demo_clock, cancel_once, monkeypatch):
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        demo_clock.at += timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(session, '_fresh_bound_facts', lambda: (_ for _ in ()).throw(OSError('offline')))
            with pytest.raises(OSError):
                runner.quote(demo_clock.quote())
    with open_runner(deployment, cancel_once, counts, authorized=False) as (_, runner):
        with pytest.raises(SafetyBlocked, match='authorization required'):
            runner.quote(demo_clock.quote())
        pending_cancel(runner, key)
        assert counts['DELETE'] == 0
    with open_runner(deployment, cancel_once, counts) as (_, runner):
        assert runner.quote(demo_clock.quote()).status is S.CANCELED
        confirmed(runner, key)


def test_crash_after_terminal_before_outbox_checkpoint_never_repeats_delete(
        deployment, demo_clock, cancel_once, monkeypatch, record_property):
    counts = dict(GET=0, POST=0, DELETE=0)
    with open_runner(deployment, cancel_once, counts) as (session, runner):
        key = runner.quote(demo_clock.quote()).intent.client_order_id
        demo_clock.at += timedelta(seconds=1)
        original = session.cancel
        def interrupted(key):
            assert original(key).status is S.CANCELED
            raise RuntimeError('crash before outbox checkpoint')
        monkeypatch.setattr(session, 'cancel', interrupted)
        with pytest.raises(RuntimeError, match='crash before outbox checkpoint'):
            runner.quote(demo_clock.quote())
        pending_cancel(runner, key)
        assert runner.execution.cancellation_confirmed(key)
    with open_runner(deployment, cancel_once, counts) as (_, runner):
        confirmed(runner, key)
        assert runner.quote(demo_clock.quote()) is None
    assert order_writes(deployment[3]) == [('POST', key), ('DELETE', key)]
    evidence(record_property, counts)


def test_paper_cancel_uses_same_durable_lifecycle(tmp_path, cancel_once, monkeypatch):
    path = tmp_path / 'paper.sqlite3'
    from factorlab.trading.paper import Quote
    with closing(PaperStore(path)) as store:
        controller = paper_controller(store, fee_rate=D('.001'))
        runner = strategy_runtime(controller, strategy=cancel_once, interval_seconds=60,
                                  deployment_id='cancel', client_id_prefix='cancel-')
        assert runner.recover().reconciled
        runner.quote(Quote('NVDAUSDT', D(100), controller.clock()))
        key = 'cancel-entry'
        controller.fill(key, D('.01'), D(99))
        controller.clock = controller.broker.clock = lambda: at
        at = runner.gateway.state.quotes['NVDAUSDT'].observed_at + timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(controller, 'cancel', lambda _: (_ for _ in ()).throw(OSError('interrupted')))
            with pytest.raises(OSError):
                runner.quote(Quote('NVDAUSDT', D(100), at))
        offer = pending_cancel(runner, key)
        economics = economic_state(runner)
        runner.pause()
    with closing(PaperStore(path)) as store:
        controller = paper_controller(store, fee_rate=D('.001'))
        controller.clock = controller.broker.clock = lambda: at + timedelta(seconds=120)
        runner = strategy_runtime(controller, strategy=cancel_once, interval_seconds=60,
                                  deployment_id='cancel', client_id_prefix='cancel-')
        assert pending_cancel(runner, key) == offer
        assert runner.resume().reconciled
        runner.quote(Quote('NVDAUSDT', D(100), controller.clock()))
        confirmed(runner, key)
        order = controller.broker.state.orders[key].snapshot
        assert order.cumulative_filled_quantity == D('.01') and order.cumulative_fee == D('.00099')
        assert economic_state(runner) == economics
        assert runner.quote(Quote('NVDAUSDT', D(100), controller.clock())) is None
        assert [event.status for _, event in store.order_events()] == [S.SUBMITTED, S.PARTIALLY_FILLED, S.CANCELED]
