"""Official wire fixtures remain offline evidence, never online acceptance."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import asyncio
import json
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_equity import CALENDAR_PHASES, PHASES
from factorlab.integrations.binance_equity.client import Client, MarketDataFailure, Response
from factorlab.integrations.binance_equity.market import stream_rows
from factorlab.integrations.binance_equity_trading.protocol import Unavailable
from factorlab.trading import Market, OrderIntent, OrderType, Side
from factorlab.trading.observations import acquire_rules
from factorlab.trading.realtime import RealtimeAccount, equity_events
from factorlab.trading.realtime_service import observe
from factorlab.trading.simulation import PAPER_SIMULATION
from factorlab.trading.storage import dumps
from test_realtime import NOW, rules


def price(phase='O', **fields):
    return dict(e='price', rates=[dict(s='OTHER', mp='C', t=0, p='999'),
        dict(s='NVDA', mp=phase, t=0, p='100', **fields)])


def calendar(phase='MARKET_OPEN', previous='PRE_MARKET', at=NOW):
    return dict(e='calendar', to=phase, ts=int(at.timestamp() * 1000), **{'from': previous})


def feed(account, wire, now, official=True):
    events = list(equity_events(wire, now))
    assert len(events) == 1
    return account._stream_event(events[0], wire, official_transport=official)


@pytest.fixture
def public_stock(tmp_path, monkeypatch):
    now = [NOW]
    raw = json.dumps(dict(symbols=[rules(Market.BINANCE_EQUITY)])).encode()
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return raw
    def get(request, timeout):
        assert request.get_method() == 'GET'
        return Reply()
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'offlinefixturekey')
    monkeypatch.setattr('factorlab.integrations.binance_equity.client.build_opener',
        lambda *_: SimpleNamespace(open=get))
    account = RealtimeAccount(tmp_path / 'stocks.sqlite', Market.BINANCE_EQUITY,
        clock=lambda: now[0], data_mode='public', simulation_policy=PAPER_SIMULATION)
    assert account.refresh_rules(network=True)
    account.control('start')
    account.event(dict(kind='quote', symbol='NVDA', time=NOW, received=NOW,
        bid='99.99', ask='100', bid_size='10', ask_size='10'))
    yield account, now
    account.close()


def intent():
    return OrderIntent(Market.BINANCE_EQUITY, 'NVDA', Side.BUY, OrderType.LIMIT,
        D('.1'), D('100'), 'p' * 32)


@pytest.mark.parametrize('phase,expected', list(PHASES.items()) + [('?', 'UNKNOWN'), (None, 'UNKNOWN')])
def test_snapshot_enums_and_independent_times(public_stock, phase, expected):
    a, now = public_stock
    assert feed(a, price(phase), now[0])
    session = a.state.runtime['session']
    assert session['phase'] == expected
    assert session['price_time'] == 0
    assert session['received'] == NOW
    assert session['time'] == int(NOW.timestamp() * 1000)
    assert session['effective_at'] is session['transition_detected_at'] is None
    assert a.state.quotes['NVDA'].price == D('100')
    if expected in {'UNKNOWN', 'CLOSED', 'OVERNIGHT'}:
        with pytest.raises(Unavailable):
            a._admit(intent())
    else:
        a._admit(intent())


@pytest.mark.parametrize('phase,expected', list(CALENDAR_PHASES.items()) + [('O', 'UNKNOWN'), ('?', 'UNKNOWN')])
def test_calendar_enums_and_detection_time(public_stock, phase, expected):
    a, now = public_stock
    wire = calendar(phase)
    _, rows = stream_rows(wire, NOW.isoformat(), 'offline')
    assert rows[0]['session'] == expected
    assert feed(a, wire, now[0])
    session = a.state.runtime['session']
    assert session['phase'] == expected
    assert session['transition_detected_at'] == NOW
    assert session['effective_at'] is session['price_time'] is None


def test_official_response_missing_min_qty_remains_unknown(public_stock, monkeypatch):
    from contextlib import contextmanager
    a, now = public_stock
    row = rules(Market.BINANCE_EQUITY)
    del row['minQty']
    body = json.dumps(dict(symbols=[row])).encode()
    @contextmanager
    def opened(request, timeout):
        assert request.get_method() == 'GET'
        yield SimpleNamespace(status=200, read=lambda: body)
    monkeypatch.setattr('factorlab.integrations.binance_equity.client.build_opener',
        lambda *_: SimpleNamespace(open=opened))
    now[0] += timedelta(seconds=1)
    assert a.refresh_rules(network=True)
    health = a.observation_health()['rules']
    assert health['provenance'] == 'VERIFIED'
    assert health['validity'] == 'UNKNOWN'
    assert health['parsed']['reason'] == 'INCOMPLETE_SYMBOL_RULES:minQty'
    assert 'minQty' not in a.state.runtime['rules']['payload']
    feed(a, price(), now[0])
    with pytest.raises(Unavailable, match='UNKNOWN_RULES'):
        a._admit(intent())


def test_recorded_target_omission_pattern_does_not_create_conflicts(public_stock):
    a, now = public_stock
    # Target occurrence pattern from the bounded 2026-11-11T20:16:32Z observation.
    for included in (True, False, False, False, True, False, False, True,
                     False, False, False, True, False, False, True, False, False):
        wire = price() if included else dict(e='price', rates=[dict(s='OTHER', mp='C')])
        for event in equity_events(wire, now[0]):
            a._stream_event(event, wire, official_transport=True)
        assert a.state.runtime['session']['phase'] == 'REGULAR'
        assert not a.state.runtime['session']['conflict']
        now[0] += timedelta(seconds=2)
    assert a.observation_health()['session']['validity'] == 'VERIFIED'


def test_calendar_updates_snapshot_duplicates_and_out_of_order(public_stock):
    a, now = public_stock
    assert feed(a, price('PRE'), now[0])
    now[0] += timedelta(seconds=1)
    wire = calendar(at=now[0])
    assert feed(a, wire, now[0])
    assert not feed(a, wire, now[0])
    assert not feed(a, calendar('CLOSED', 'POST_MARKET', NOW), now[0])
    a._admit(intent())
    now[0] += timedelta(seconds=1)
    assert feed(a, calendar('CLOSED', 'MARKET_OPEN', now[0]), now[0])
    with pytest.raises(Unavailable, match='SESSION_NOT_TRADABLE'):
        a._admit(intent())


def test_conflicting_snapshot_cannot_reauthorize_until_new_transition(public_stock):
    a, now = public_stock
    feed(a, price(), now[0])
    now[0] += timedelta(seconds=1)
    feed(a, price('C'), now[0])
    assert a.state.runtime['session']['conflict']
    assert 'session' not in a._verified_observations
    with pytest.raises(Unavailable):
        a._admit(intent())
    now[0] += timedelta(seconds=1)
    feed(a, price(), now[0])
    assert a.state.runtime['session']['phase'] == 'UNKNOWN'
    now[0] += timedelta(seconds=1)
    feed(a, calendar(at=now[0]), now[0])
    a._admit(intent())
    from factorlab.integrations.binance_equity_trading.requests import live_submit, live_cancel
    for mutation in (live_submit, live_cancel):
        with pytest.raises(PermissionError, match='NO LIVE MONEY'):
            mutation(network=True, live=True, tokenize=False)


def test_unknown_initial_snapshot_can_be_replaced_by_valid_observation(public_stock):
    a, now = public_stock
    feed(a, price(None), now[0])
    with pytest.raises(Unavailable):
        a._admit(intent())
    now[0] += timedelta(seconds=1)
    feed(a, price(), now[0])
    a._admit(intent())


def test_same_timestamp_calendar_conflict_and_unknown_revoke(public_stock):
    a, now = public_stock
    wire = calendar()
    feed(a, wire, now[0])
    feed(a, calendar('CLOSED'), now[0])
    assert a.state.runtime['session']['phase'] == 'UNKNOWN'
    assert 'session' not in a._verified_observations
    now[0] += timedelta(seconds=1)
    feed(a, calendar(at=now[0]), now[0])
    now[0] += timedelta(seconds=1)
    feed(a, price('?'), now[0])
    with pytest.raises(Unavailable):
        a._admit(intent())


@pytest.mark.parametrize('wire', [dict(e='price', rates=[]),
    dict(e='price', rates=[dict(s='OTHER', mp='O')])])
def test_omitted_target_neither_authorizes_nor_revokes_or_renews(public_stock, wire):
    a, now = public_stock
    assert list(equity_events(wire, now[0])) == []
    assert a.state.runtime['session'] is None
    with pytest.raises(Unavailable, match='MISSING_SESSION'):
        a._admit(intent())
    feed(a, price(), now[0])
    session = dumps(a.state.runtime['session'])
    now[0] += timedelta(seconds=1)
    assert list(equity_events(wire, now[0])) == []
    assert dumps(a.state.runtime['session']) == session
    a._admit(intent())
    now[0] += timedelta(seconds=300)
    assert list(equity_events(wire, now[0])) == []
    assert a.refresh_rules(network=True)
    with pytest.raises(Unavailable, match='STALE_OR_FUTURE'):
        a._admit(intent())


def test_ambiguous_target_revokes(public_stock):
    wire = dict(e='price', rates=[dict(s='NVDA', mp='O'), dict(s='NVDA', mp='C')])
    a, now = public_stock
    feed(a, price(), now[0])
    now[0] += timedelta(seconds=1)
    feed(a, wire, now[0])
    assert a.state.runtime['session']['phase'] == 'UNKNOWN'
    assert a.state.runtime['session']['evidence']['raw']['rates'] == []
    with pytest.raises(Unavailable):
        a._admit(intent())


def test_snapshot_persistence_is_target_only_and_restarts_reacquire(tmp_path):
    now = [NOW]
    path = tmp_path / 'stocks.sqlite'
    a = RealtimeAccount(path, Market.BINANCE_EQUITY, clock=lambda: now[0],
        data_mode='public', simulation_policy=PAPER_SIMULATION)
    wire = price()
    wire['rates'] += [dict(s='UNRELATED' + str(n), p='1', mp='C') for n in range(2000)]
    feed(a, wire, now[0])
    stored = dumps(a.store.load())
    assert 'OTHER' not in stored and 'UNRELATED' not in stored
    assert len(stored) < 20000
    assert a.state.runtime['session']['evidence']['raw']['scope']['returned_symbols'] == 2002
    a.close()
    restored = RealtimeAccount(path, Market.BINANCE_EQUITY, clock=lambda: now[0],
        data_mode='public', simulation_policy=PAPER_SIMULATION)
    try:
        assert restored.state.runtime['session'] is None
        assert restored.state.runtime['rules'] is None
        assert not restored._verified_observations
        assert feed(restored, wire, now[0])
        assert restored.observation_health()['session']['validity'] == 'VERIFIED'
    finally:
        restored.close()


def test_expiry_and_stale_transition_fail_closed(public_stock):
    a, now = public_stock
    feed(a, price(), now[0])
    now[0] += timedelta(seconds=301)
    a.event(dict(kind='quote', symbol='NVDA', time=now[0], received=now[0],
        bid='99.99', ask='100', bid_size='10', ask_size='10'))
    # Refresh rules so this rejection specifically exercises phase expiry.
    assert a.refresh_rules(network=True)
    with pytest.raises(Unavailable, match='STALE_OR_FUTURE'):
        a._admit(intent())
    assert not feed(a, calendar(at=NOW), now[0])
    assert a.state.runtime['session'] is None
    assert 'session' not in a._verified_observations


def test_replay_and_injected_connector_cannot_authorize(public_stock):
    a, now = public_stock
    feed(a, price(), now[0], official=False)
    with pytest.raises(Unavailable, match='UNVERIFIED_SESSION_PROVENANCE'):
        a._admit(intent())
    now[0] += timedelta(seconds=1)
    value = next(equity_events(price(), now[0]))
    value['evidence'] = dict(provenance='OFFICIAL_TRANSPORT')
    assert a.event(value)
    with pytest.raises(Unavailable, match='UNVERIFIED_SESSION_PROVENANCE'):
        a._admit(intent())


@pytest.mark.parametrize('status,body,reason,code', [
    (401, b'{}', 'PERMISSION_DENIED', None),
    (403, b'{"code":-2015,"msg":"denied"}', 'PERMISSION_DENIED', -2015),
    (451, b'{}', 'REGION_RESTRICTED', None),
    (403, b'{"msg":"Service unavailable from your location"}', 'REGION_RESTRICTED', None),
    (403, b'{}', 'HTTP_ERROR', None), (500, b'bad upstream', 'HTTP_ERROR', None)])
def test_safe_rule_failure_categories(monkeypatch, status, body, reason, code):
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'fixturekey')
    sink = SimpleNamespace(raw=lambda *_args, **_kw: pytest.fail('error body must not be persisted'))
    client = Client(network=True, request=lambda *_: Response(status, body))
    with pytest.raises(MarketDataFailure) as caught:
        client._get('exchangeInfo', 'NVDA', sink)
    assert caught.value.diagnostic() == dict(reason=reason, http_status=status, exchange_code=code)
    assert 'fixturekey' not in str(caught.value)


def test_rule_key_missing_network_and_target_absent(monkeypatch):
    monkeypatch.delenv('BINANCE_EQUITY_API_KEY', raising=False)
    with pytest.raises(MarketDataFailure, match='MISSING_OR_INVALID_API_KEY'):
        acquire_rules(Market.BINANCE_EQUITY, 'NVDA', network=True,
            stocks_transport=lambda *_: pytest.fail('missing key must not send request'))
    monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'fixturekey')
    def failed(*_):
        raise OSError('upstream may contain fixturekey')
    with pytest.raises(MarketDataFailure, match='NETWORK_ERROR') as caught:
        acquire_rules(Market.BINANCE_EQUITY, 'NVDA', network=True, stocks_transport=failed)
    assert 'fixturekey' not in str(caught.value)
    with pytest.raises(MarketDataFailure, match='TARGET_SYMBOL_ABSENT'):
        acquire_rules(Market.BINANCE_EQUITY, 'NVDA', network=True,
            stocks_transport=lambda *_: Response(200, b'{"symbols":[]}'))


def test_single_socket_subscribes_price_and_injection_does_not_certify(tmp_path):
    calls, sent = [], []
    wire = price()
    wire['rates'] += [dict(s='OTHER' + str(n), mp='C', p='1') for n in range(3000)]
    body = json.dumps(wire)
    assert len(body) > 65536
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def send(self, text):
            sent.append(json.loads(text))
        async def recv(self):
            if len(calls) == 1:
                calls.append('delivered')
                return body
            raise OSError('end fixture')
    def connector(url, **options):
        calls.append(options)
        return Socket()
    a = RealtimeAccount(tmp_path / 'socket.sqlite', Market.BINANCE_EQUITY,
        data_mode='public', clock=lambda: datetime.now(timezone.utc))
    try:
        result = asyncio.run(observe(a, network=True, seconds=1, reconnects=0, connector=connector))
        assert sent == [dict(method='SUBSCRIBE', params=['NVDA@quote', 'calendar', 'price'], id=1)]
        assert calls[0]['max_size'] == 4 * 1024 * 1024
        assert result['diagnostics']['phase_observations'] == {'SNAPSHOT': 1}
        assert result['diagnostics']['last_phase_observation']['validity'] == 'UNVERIFIED'
        assert a.state.runtime['session'] is None
        assert not a._verified_observations
        assert 'OTHER' not in dumps(a.store.load())
    finally:
        a.close()


def test_missing_calendar_timestamp_revokes_and_disconnect_reacquires(public_stock):
    a, now = public_stock
    assert feed(a, price(), now[0])
    assert not feed(a, price(), now[0])
    now[0] += timedelta(seconds=1)
    wire = calendar()
    wire['ts'] = None
    assert feed(a, wire, now[0])
    with pytest.raises(Unavailable):
        a._admit(intent())
    a.disconnect()
    assert 'session' not in a._verified_observations
    assert feed(a, price(), now[0])
    assert a.observation_health()['session']['validity'] == 'VERIFIED'
    a._admit(intent())
