"""Deterministic, network-free Alpaca stock stream contract fixtures."""
import asyncio
from datetime import datetime, timezone
import json

import pytest

from factorlab.integrations.alpaca.market_data.stream import (
    AlpacaStream, FEED_URLS, StreamDisconnected, StreamProtocolError,
    StreamRecoveryError,
)

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
STAMP = '2026-11-24T15:00:00.123456789Z'


def trade(**fields):
    return dict({'T': 't', 'S': 'AAPL', 't': STAMP, 'i': 7, 'p': 100, 's': 1}, **fields)


class Socket:
    def __init__(self, frames=(), *, ack=None):
        self.frames = [
            [{'T': 'success', 'msg': 'connected'}],
            [{'T': 'success', 'msg': 'authenticated'}],
            [ack or {'T': 'subscription', 'trades': ['AAPL']}],
            *frames,
        ]
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.frames:
            raise EOFError('closed')
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return json.dumps(frame)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Connector:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        socket = self.sockets.pop(0)
        if isinstance(socket, BaseException):
            raise socket
        return socket


def stream(connector, **options):
    return AlpacaStream(api_key='fixture-key', secret_key='fixture-secret',
                        feed=options.pop('feed', 'iex'),
                        subscriptions=options.pop('subscriptions', {'trades': ['AAPL']}),
                        connector=connector, clock=lambda: NOW, **options)


@pytest.mark.parametrize('feed', FEED_URLS)
def test_explicit_feed_auth_subscription_and_envelope(feed):
    async def scenario():
        socket = Socket([[trade()]])
        connector = Connector(socket)
        client = stream(connector, feed=feed)
        iterator = client.events()
        event = await anext(iterator)
        assert connector.urls == [FEED_URLS[feed]]
        assert 'fixture' not in connector.urls[0]
        assert socket.sent == [
            {'action': 'auth', 'key': 'fixture-key', 'secret': 'fixture-secret'},
            {'action': 'subscribe', 'trades': ['AAPL']},
        ]
        assert event.event_at == STAMP
        assert event.received_at == NOW and event.received_at.utcoffset().total_seconds() == 0
        assert event.source == feed and event.channel == 'trades'
        assert event.payload == trade()
        assert client.recovered
        await iterator.aclose()
        assert socket.closed and not client.recovered
    asyncio.run(scenario())


def test_reconnect_restores_subscriptions_and_awaits_recovery():
    async def scenario():
        first = Socket([[trade()]])
        second = Socket([[trade(), trade(i=8)]])
        connector = Connector(first, second)
        delays, gaps = [], []
        client = None

        async def sleep(delay):
            assert not client.recovered
            delays.append(delay)

        async def recover(**gap):
            assert not client.recovered
            assert len(second.sent) == 2
            gaps.append(gap)
            return True

        client = stream(connector, sleep=sleep, recover_gap=recover)
        iterator = client.events()
        assert (await anext(iterator)).payload['i'] == 7
        assert (await anext(iterator)).payload['i'] == 8
        assert client.recovered
        assert delays == [1.0]
        assert second.sent == first.sent
        assert gaps == [dict(start=NOW, end=NOW, feed='iex', subscriptions={'trades': ('AAPL',)})]
        await iterator.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', [None, False, 'error'])
def test_gap_recovery_is_mandatory_and_failure_is_terminal(outcome):
    async def scenario():
        async def recover(**gap):
            if outcome == 'error':
                raise ValueError('fixture-secret')
            return outcome
        client = stream(Connector(Socket(), Socket([[trade()]])),
                        recover_gap=None if outcome is None else recover,
                        sleep=lambda _: asyncio.sleep(0))
        with pytest.raises(StreamRecoveryError) as caught:
            await anext(client.events())
        assert 'fixture-secret' not in str(caught.value)
        assert not client.recovered
    asyncio.run(scenario())


def test_reconnect_budget_and_capped_backoff():
    async def scenario():
        delays = []
        async def sleep(delay):
            delays.append(delay)
        connector = Connector(*(OSError('fixture-secret') for _ in range(5)))
        client = stream(connector, max_reconnects=4, backoff=2, max_backoff=5, sleep=sleep)
        with pytest.raises(StreamDisconnected) as caught:
            await anext(client.events())
        assert delays == [2, 4, 5, 5]
        assert len(connector.urls) == 5
        assert 'fixture-secret' not in str(caught.value)
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', [0, 1, 2])
def test_ack_rejection_does_not_retry_or_leak_server_messages(stage):
    async def scenario():
        socket = Socket([[trade()]])
        socket.frames[stage] = [{'T': 'error', 'code': 401, 'msg': 'fixture-secret'}]
        connector = Connector(socket)
        client = stream(connector)
        with pytest.raises(StreamProtocolError) as caught:
            await anext(client.events())
        assert 'fixture-secret' not in str(caught.value)
        assert len(connector.urls) == 1
        assert socket.closed
    asyncio.run(scenario())


def test_subscription_mismatch_rejected():
    async def scenario():
        client = stream(Connector(Socket(ack={'T': 'subscription', 'trades': ['MSFT']})))
        with pytest.raises(StreamProtocolError, match='differs'):
            await anext(client.events())
    asyncio.run(scenario())


def test_channel_specific_dedup_keeps_revisions_corrections_and_cancel_errors():
    async def scenario():
        correction = {'T': 'c', 'S': 'AAPL', 't': STAMP, 'oi': 7, 'ci': 8, 'cp': 99}
        cancel = {'T': 'x', 'S': 'AAPL', 't': STAMP, 'i': 8, 'a': 'C'}
        bar = {'T': 'b', 'S': 'AAPL', 't': STAMP, 'c': 100}
        update = dict(bar, T='u', c=101)
        frames = [[trade(), trade(), correction, correction, cancel, bar, update, dict(update, c=102)]]
        socket = Socket(frames, ack={'T': 'subscription', 'trades': ['AAPL'],
                                    'bars': ['AAPL'], 'updatedBars': ['AAPL']})
        client = stream(Connector(socket), subscriptions={
            'trades': ['AAPL'], 'bars': ['AAPL'], 'updatedBars': ['AAPL']})
        iterator = client.events()
        events = [await anext(iterator) for _ in range(6)]
        assert [event.channel for event in events] == [
            'trades', 'corrections', 'cancelErrors', 'bars', 'updatedBars', 'updatedBars']
        assert events[1].payload == correction and events[2].payload == cancel
        await iterator.aclose()
    asyncio.run(scenario())


def test_lru_state_is_bounded_and_old_events_can_reappear():
    async def scenario():
        client = stream(Connector(Socket([[trade(), trade(i=8), trade(i=9), trade()]])),
                        dedup_capacity=2)
        iterator = client.events()
        assert [(await anext(iterator)).payload['i'] for _ in range(4)] == [7, 8, 9, 7]
        assert len(client._seen) == 2
        await iterator.aclose()
    asyncio.run(scenario())


def test_receive_timeout_is_bounded_and_closes_socket():
    async def scenario():
        class HangingSocket(Socket):
            async def recv(self):
                await asyncio.Future()
        socket = HangingSocket()
        client = stream(Connector(socket), receive_timeout=.001, max_reconnects=0)
        with pytest.raises(StreamDisconnected):
            await anext(client.events())
        assert socket.closed and not client.recovered
    asyncio.run(scenario())


@pytest.mark.parametrize('event', [trade(S='MSFT'), trade(t=None), {'T': 'unknown'}])
def test_unsubscribed_or_malformed_event_is_not_fabricated(event):
    async def scenario():
        client = stream(Connector(Socket([[event]])))
        with pytest.raises(StreamProtocolError):
            await anext(client.events())
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [dict(feed='auto'), dict(subscriptions={}),
    dict(subscriptions={'trades': 'AAPL'}), dict(subscriptions={'orders': ['AAPL']}),
    dict(subscriptions={'trades': ['AAPL', 'AAPL']}), dict(receive_timeout=0),
    dict(max_reconnects=-1), dict(dedup_capacity=0)])
def test_invalid_configuration_rejected_before_connect(options):
    with pytest.raises(ValueError):
        stream(Connector(), **options)
