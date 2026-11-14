"""Signal lifetime remains independent of trigger and executable-book freshness."""
from contextlib import closing
from datetime import timedelta
from dataclasses import replace
from decimal import Decimal as D

import pytest

from test_binance_demo_strategy_session import deployment, wired
from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.trading.paper import Quote
from factorlab.trading.strategy import ClosedBar, DualMAStrategy


@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('reduce_only', [False, True])
@pytest.mark.parametrize('trigger,book_time,decision,expired', [
    (57, 67, 67, True),
    (50, 55, 55, False),
    (57, 59, 60, True),
    (57, 60, 60, True),
    (60, 60, 60, True),
])
def test_session_signal_deadline_and_restart(deployment, monkeypatch, preview,
        trigger, book_time, decision, expired, reduce_only):
    root, store, campaign, fake = deployment
    if reduce_only:
        fake.quantity, fake.entry = D('.03'), D(100)
    counts = dict(GET=0, POST=0, DELETE=0)
    strategy = DualMAStrategy(interval_seconds=60, max_weight=D('.004'))
    configuration = dict(client_id_prefix=campaign['prefix'] + '-', preview=preview)
    with closing(DemoRuntime(root)) as runtime:
        with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
            runner = DemoStrategyRuntime(session, strategy, expect=expect, **configuration)
            runner.recover()
            clock = [NOW + timedelta(seconds=trigger)]
            monkeypatch.setattr(session.controller, 'clock', lambda: clock[0])
            monkeypatch.setattr(session.gateway, 'clock', lambda: clock[0])
            monkeypatch.setattr(session, '_now', lambda: clock[0])
            request = session.client.request
            def current_market(method, path, *args, **kwargs):
                result = request(method, path, *args, **kwargs)
                if path.endswith('premiumIndex'):
                    result = dict(result, time=int(clock[0].timestamp() * 1000))
                return result
            monkeypatch.setattr(session.client, 'request', current_market)
            read_facts = session.adapter.read_facts
            def fresh_facts():
                facts = replace(read_facts(), observed_at=clock[0])
                session.adapter.facts = facts
                return facts
            monkeypatch.setattr(session.adapter, 'read_facts', fresh_facts)
            for i in range(21):
                opened = NOW - timedelta(minutes=21-i)
                price = D(100 if i < 20 else 110)
                runner.closed_bar(ClosedBar('NVDAUSDT', opened, opened + timedelta(minutes=1),
                    price, price, price, price, D(100), 60, strategy.describe()['source']))
            signal = dict(session.gateway.state.runtime['pending'])
            if reduce_only:
                signal.update(direction='SELL', target_weight=D(0))
                session.controller._atomic(lambda: session.gateway.state.runtime.update(pending=signal))
            def refresh():
                clock[0] = NOW + timedelta(seconds=decision)
                session.adapter.facts = replace(session.adapter.facts, observed_at=clock[0])
                return dict(askPrice=D(100), bidPrice=D(100), time_utc=NOW + timedelta(seconds=book_time))
            monkeypatch.setattr(session, 'prepare_limit_book', refresh)
            event = Quote('NVDAUSDT', D(100), NOW + timedelta(seconds=trigger))
            runner.quote(event)
            state = session.gateway.state.runtime
            assert state['pending'] is None
            if expired:
                assert state['last_rejection'] == 'SIGNAL_EXPIRED'
                assert 'last_order' not in state
                assert 'target_position' not in state
                assert session.controller.store.event_payload('target:' + signal['signal_id']) is None
            else:
                assert state['last_order']['intent']['time_in_force'] == 'GTC'
                assert state['last_order']['intent']['reduce_only'] is reduce_only
            assert counts['POST'] == int(not expired and not preview)
        if expired:
            with open_session(root, store, runtime, counts, campaign=campaign) as (session, expect):
                runner = DemoStrategyRuntime(session, strategy, expect=expect, **configuration)
                runner.recover()
                assert session.gateway.state.runtime['last_rejection'] == 'SIGNAL_EXPIRED'
                assert session.gateway.state.runtime['pending'] is None
                runner.quote(event)
                assert 'last_order' not in session.gateway.state.runtime
                assert counts['POST'] == 0


@pytest.mark.parametrize('preview', [False, True])
def test_offline_decision_clock_expires_signal(preview):
    from test_binance_demo_execution import setup
    from test_binance_demo_strategy_session import bars
    adapter, gateway, controller, fake = setup()
    runner = DemoStrategyRuntime(controller, DualMAStrategy(), preview=preview)
    runner.recover()
    bars(runner)
    # The trigger is fresh on arrival, but the atomic decision is at expiry.
    controller.clock = gateway.clock = lambda: NOW + timedelta(hours=1)
    runner.quote(Quote('NVDAUSDT', D(100), NOW + timedelta(seconds=3597)))
    assert gateway.state.runtime['last_rejection'] == 'SIGNAL_EXPIRED'
    assert gateway.state.runtime['pending'] is None
    assert 'last_order' not in gateway.state.runtime
    assert not [call for call in fake.calls if call[0] == 'POST']
