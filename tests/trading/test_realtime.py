from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import asyncio
import json
import subprocess
import sys

import pytest

from factorlab.trading import Market, OrderIntent, OrderType, Side, OrderStatus
from factorlab.trading.realtime import RealtimeAccount, PersistenceConflict, equity_events
from factorlab.trading.control import RiskRejected
from factorlab.trading.realtime_service import observe
from factorlab.trading.storage import PaperStore
from factorlab.integrations.binance_futures_trading.market import parse
from factorlab.integrations.binance_futures_trading.protocol import FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked
from factorlab.integrations.binance_equity_trading.protocol import Unavailable

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def rules(market):
    if market == Market.BINANCE_EQUITY:
        return dict(symbol='NVDA', tradability='BUY_SELL', stepSize='0.001', minQty='0.001',
            maxQty='1000', minNotional='1', maxNotional='100000', fractionable=True,
            fractionableEh=True, extendedSession=True, overnightSupported=True, maxNumOrders=100,
            multiplierDown='0.5', multiplierUp='1.5')
    return dict(symbol=dict(symbol='NVDAUSDT', status='TRADING', quoteAsset='USDT', marginAsset='USDT', orderTypes=['MARKET', 'LIMIT'], filters=[
        dict(filterType='LOT_SIZE', minQty='0.01', maxQty='100', stepSize='0.01'),
        dict(filterType='MARKET_LOT_SIZE', minQty='0.01', maxQty='100', stepSize='0.01'),
        dict(filterType='PRICE_FILTER', minPrice='0.01', maxPrice='10000', tickSize='0.01'),
        dict(filterType='MIN_NOTIONAL', notional='5')]), bracket=dict(symbol='NVDAUSDT', brackets=[
        dict(notionalFloor='0', notionalCap='10000', initialLeverage=1)]))


class Harness:
    def __init__(self, tmp, market=Market.BINANCE_USDM):
        self.time = NOW
        self.account = RealtimeAccount(tmp, market, clock=lambda: self.time)
        self.market = market
        self.sequence = 0
        self.send('rules', payload=rules(market), reference='100', source='SYNTHETIC_OFFLINE_ONLY')
        if market == Market.BINANCE_EQUITY:
            self.send('session', phase='REGULAR')
        else:
            self.send('mark', price='100')
        self.account.control('start')

    def send(self, kind, **fields):
        value = dict(kind=kind, symbol=self.account.symbol, time=self.time, received=self.time, **fields)
        return self.account.event(value)

    def quote(self, price, size='1'):
        self.time += timedelta(seconds=1)
        self.sequence += 1
        if self.market == Market.BINANCE_USDM:
            self.send('mark', price=price)
            value = dict(kind='trade', symbol='NVDAUSDT', time=self.time, received=self.time,
                         price=price, size=size, sequence=self.sequence)
        else:
            value = dict(kind='quote', symbol='NVDA', time=self.time, received=self.time,
                         bid=str(D(price) - D('.01')), ask=price, bid_size=size, ask_size=size)
        self.account.event(value)
        return value


@pytest.mark.parametrize('market', list(Market))
def test_strategy_partial_cancel_duplicate_and_restart(tmp_path, market):
    path = tmp_path / 'paper.sqlite'
    h = Harness(path, market)
    h.quote('100')
    h.quote('101')
    a = h.account
    assert a.health()['signals'] == a.health()['orders'] == 1
    assert not a.state.positions  # signal quote does not confirm a fill
    value = h.quote('100', '.4')
    assert a.health()['fills'] == 1
    assert a.state.positions[a.symbol].quantity == D('.04')
    assert not a.event(value)
    order_id = next(iter(a.state.orders))
    confirmation = a.state.confirmations[order_id]
    before = a.controller.account()
    assert not a.replay(confirmation)
    assert before == a.controller.account()
    a.control('kill')
    expected_cash = a.state.cash.copy()
    a.close()
    restored = RealtimeAccount(path, market, clock=lambda: h.time)
    assert restored.state.cash == expected_cash
    assert restored.state.positions[restored.symbol].quantity == D('.04')
    assert restored.state.kill_switch and restored.state.reconciled
    assert restored.state.runtime['signals'] == 1
    assert restored.state.runtime['last_valid'] == h.time
    assert not restored.event(value)
    restored.cancel(order_id)
    assert restored.state.orders[order_id].snapshot.status == OrderStatus.CANCELED
    restored.close()


@pytest.mark.parametrize('missing', ['rules', 'session'])
def test_stock_unknown_gate(tmp_path, missing):
    h = Harness(tmp_path / 's.sqlite', Market.BINANCE_EQUITY)
    h.account._change(lambda: h.account.state.runtime.update({missing: None}))
    h.quote('100'); h.quote('101')
    assert h.account.health()['orders'] == 0
    assert h.account.health()['rejected'] == 1
    h.account.close()


def test_stale_stream_isolation_and_future_timestamp(tmp_path):
    f, s = Harness(tmp_path / 'f'), Harness(tmp_path / 's', Market.BINANCE_EQUITY)
    f.quote('100'); s.quote('100')
    f.time += timedelta(seconds=6)
    f.account.tick()
    assert f.account.health()['reason'] == 'STALE_QUOTE'
    s.quote('101'); s.quote('100')
    assert s.account.health()['fills'] == 1
    assert f.account.health()['orders'] == 0
    assert not s.account.event(dict(kind='quote', symbol='NVDA', time=s.time + timedelta(seconds=1),
        received=s.time, ask='100', bid='99', bid_size='1', ask_size='1'))
    assert s.account.health()['status'] == 'PAUSED'
    f.account.close(); s.account.close()


def test_persistence_conflict_and_corruption(tmp_path):
    h = Harness(tmp_path / 'f')
    h.quote('100')
    other = PaperStore(tmp_path / 'f')
    with other.transaction():
        state = other.load()
        state.cash['USDT'] += 1
        other.save(state)
    with pytest.raises(PersistenceConflict):
        h.quote('101')
    assert h.account.health()['reason'] == 'PERSISTENCE_CONFLICT'
    h.account.close(); other.close()
    with pytest.raises(PersistenceConflict, match='checkpoint'):
        RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM, clock=lambda: h.time)


def test_unknown_without_confirmation_stays_frozen(tmp_path):
    h = Harness(tmp_path / 'f')
    h.quote('100'); h.quote('101')
    key = next(iter(h.account.state.orders))
    def uncertain():
        h.account.controller.broker.mark_unknown(key)
        h.account.state.confirmations.pop(key)
    h.account._change(uncertain)
    h.account.close()
    a = RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM, clock=lambda: h.time)
    assert not a.state.reconciled
    assert a.state.orders[key].snapshot.reconcile_required
    a.control('start')
    with pytest.raises(Unavailable, match='QUERY_ONLY'):
        a.submit(a.state.orders[key].snapshot.intent)
    a.close()


def test_sequence_gap_and_mark_never_executes(tmp_path):
    h = Harness(tmp_path / 'f')
    h.quote('100'); h.quote('101')
    h.time += timedelta(seconds=1)
    h.send('mark', price='90')
    assert h.account.health()['fills'] == 0
    h.sequence += 1
    h.quote('100')
    assert h.account.health()['reason'] == 'TRADE_SEQUENCE_GAP'
    assert h.account.health()['fills'] == 0
    h.account.close()


def test_public_parser_and_equity_identity():
    ms = int(NOW.timestamp() * 1000)
    trade = parse(dict(e='aggTrade', s='NVDAUSDT', E=ms, T=ms, p='100', q='.1', a=2), NOW)
    assert trade['kind'] == 'trade' and trade['sequence'] == 2
    assert parse(dict(e='markPriceUpdate', s='NVDAUSDT', E=ms, p='99'), NOW)['kind'] == 'mark'
    with pytest.raises(ValueError):
        parse(dict(e='aggTrade', s='NVDA', E=ms, T=ms, p='100', q='.1', a=2), NOW)
    with pytest.raises(ValueError):
        parse(dict(e='aggTrade', s='NVDAUSDT', E=ms+1, T=ms+1, p='100', q='.1', a=2), NOW)
    rows = list(equity_events(dict(e='quote', s='NVDA', E=ms, T=ms, bp='99', ap='100', bs='1', **{'as': '1'}), NOW))
    assert rows[0]['kind'] == 'quote' and rows[0]['symbol'] == 'NVDA'


def test_real_trade_paths_blocked():
    c = FuturesClient(transport=ReadOnlyNetworkTransport(permit_account_read=True))
    for method in ['POST', 'DELETE']:
        with pytest.raises(SafetyBlocked):
            c.request(method, '/fapi/v1/order')


@pytest.mark.parametrize('market', list(Market))
def test_whole_process_recovery(tmp_path, market):
    h = Harness(tmp_path / 'f', market)
    h.quote('100'); h.quote('101'); h.quote('100', '.4')
    currency = 'USDT' if market == Market.BINANCE_USDM else 'USD'
    expected = str(h.account.state.cash[currency])
    h.account.close()
    code = '''from datetime import datetime, timezone
from factorlab.trading.realtime import RealtimeAccount
from factorlab.trading import Market
import sys, json
a = RealtimeAccount(sys.argv[1], Market(sys.argv[2]), clock=lambda: datetime(2026,11,12,12,0,3,tzinfo=timezone.utc))
print(json.dumps([str(next(iter(a.state.cash.values()))), str(a.state.positions[a.symbol].quantity), a.state.reconciled]))
a.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'f'), market.value], capture_output=True, text=True, check=True)
    cash, quantity, reconciled = json.loads(result.stdout)
    assert D(cash) == D(expected) and D(quantity) == D('.04') and reconciled


def test_public_opt_in_and_disconnect_budget(tmp_path):
    import asyncio
    h = Harness(tmp_path / 'f')
    with pytest.raises(PermissionError):
        asyncio.run(observe(h.account, seconds=1))
    calls = []
    def broken(url, **options):
        calls.append((url, options))
        raise OSError('offline')
    result = asyncio.run(observe(h.account, seconds=1, network=True, reconnects=2, connector=broken))
    assert len(calls) == 3 and result['failures'] == 3
    assert all(call[0] == 'wss://fstream.binance.com/market/stream?streams=nvdausdt@aggTrade/nvdausdt@markPrice@1s' for call in calls)
    assert result['diagnostics']['connection_attempts'] == 3
    assert result['diagnostics']['handshakes'] == 0
    assert result['diagnostics']['errors'] == {'connection:OSError': 3}
    assert result['diagnostics']['status'] == 'CONNECTION_FAILED'
    assert h.account.health()['status'] == 'PAUSED'
    h.account.close()


@pytest.mark.parametrize('server_offset', [0, 1])
def test_stock_source_time_can_repeat_with_new_server_event(tmp_path, server_offset):
    h = Harness(tmp_path / 's', Market.BINANCE_EQUITY)
    ms = int(h.time.timestamp() * 1000)
    def event(ask, emitted):
        h.time = NOW + timedelta(milliseconds=emitted)
        return next(equity_events(dict(e='quote', s='NVDA', E=ms + emitted, T=ms,
            bp='99', ap=ask, bs='1', **{'as': '1'}), h.time))
    first, second = event('100', 1), event('101', 1 + server_offset)
    assert h.account.event(first)
    assert h.account.event(second)
    assert not h.account.event(second)
    assert not h.account.event(first)
    assert not h.account.conflict
    assert h.account.health()['signals'] == 1
    h.account.close()


@pytest.mark.parametrize('market', list(Market))
def test_stale_quote_rejected_at_shared_risk_gate(tmp_path, market):
    h = Harness(tmp_path / 'a', market)
    h.quote('100')
    h.time += timedelta(seconds=6)
    intent = OrderIntent(market, h.account.symbol, Side.BUY, OrderType.MARKET,
                         D('.1'), None, 'manual000000000000000000000000000')
    assert h.account.controller.assess(intent).reason == 'STALE_QUOTE'
    h.account.close()


@pytest.mark.parametrize('market', list(Market))
def test_limit_slippage_and_market_assumptions(tmp_path, market):
    h = Harness(tmp_path / 'a', market)
    h.quote('100')
    key = 'manual000000000000000000000000000'
    intent = OrderIntent(market, h.account.symbol, Side.BUY, OrderType.LIMIT,
                         D('.1'), D('100'), key)
    h.account.submit(intent)
    h.quote('100')
    assert h.account.health()['fills'] == 0  # slipped price breaches the limit
    h.quote('99')
    assert h.account.health()['fills'] == 1
    event = h.account.state.confirmations[key]
    assert event.fill_price == D('99.0099')
    assert event.cumulative_fee == D('.1') * D('99.0099') * D('.001')
    h.account.close()


def test_market_stock_fixed_notional_and_cancel(tmp_path):
    h = Harness(tmp_path / 'a', Market.BINANCE_EQUITY)
    h.quote('100')
    key = 'manual000000000000000000000000000'
    h.account.submit(OrderIntent(h.market, 'NVDA', Side.BUY, OrderType.MARKET, D('.1'), None, key))
    h.quote('101', '.4')
    assert h.account.state.confirmations[key].fill_price == D('100')
    h.account.cancel(key)
    h.quote('99')
    assert h.account.state.orders[key].snapshot.status == OrderStatus.CANCELED
    assert h.account.state.positions['NVDA'].quantity == D('.04')
    h.account.close()


def test_configuration_rejection_preserves_restartable_state(tmp_path):
    h = Harness(tmp_path / 'a')
    h.quote('100'); h.quote('101')
    h.account.close()
    with pytest.raises(PersistenceConflict, match='configuration'):
        RealtimeAccount(tmp_path / 'a', h.market, clock=lambda: h.time, data_mode='public')
    a = RealtimeAccount(tmp_path / 'a', h.market, clock=lambda: h.time)
    assert a.state.reconciled and len(a.state.orders) == 1
    a.close()


def test_journal_conflict_blocks_restart(tmp_path):
    h = Harness(tmp_path / 'a')
    h.quote('100'); h.quote('101')
    event_id = next(iter(h.account.state.confirmations.values())).event_id
    h.account.close()
    store = PaperStore(tmp_path / 'a')
    store.db.execute('DELETE FROM events WHERE id=?', (event_id,))
    store.close()
    with pytest.raises(PersistenceConflict, match='journal'):
        RealtimeAccount(tmp_path / 'a', h.market, clock=lambda: h.time)


def test_store_capacity_failure_rolls_back_signal_and_account(tmp_path):
    h = Harness(tmp_path / 'a')
    h.quote('100'); h.quote('101')
    cash = h.account.state.cash.copy()
    h.account.store.max_events = 1
    with pytest.raises(ValueError, match='budget'):
        h.quote('100')
    assert h.account.state.cash == cash and not h.account.state.positions
    assert h.account.conflict
    h.account.close()


def test_malformed_public_feed_does_not_cancel_other_market(tmp_path):
    import asyncio
    f = RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM)
    s = RealtimeAccount(tmp_path / 's', Market.BINANCE_EQUITY)
    f.control('start'); s.control('start')
    class Socket:
        def __init__(self, body):
            self.body = body
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def send(self, message):
            assert 'SUBSCRIBE' in message and 'NVDA@quote' in message
        async def recv(self):
            if self.body is None:
                raise OSError('stream ended')
            result, self.body = self.body, None
            return result
    def connector(url, **options):
        assert options['max_queue'] == 8 and options['ping_timeout'] == 5
        if 'fstream.binance.com' in url:
            return Socket('{malformed')
        ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return Socket(json.dumps(dict(e='quote', s='NVDA', E=ms, T=ms,
            bp='99', ap='100', bs='1', **{'as': '1'})))
    async def run():
        return await asyncio.gather(*(observe(a, network=True, seconds=1, reconnects=0,
            connector=connector) for a in (f, s)))
    results = asyncio.run(run())
    assert results[0]['error'] == 'JSONDecodeError'
    assert results[0]['diagnostics']['handshakes'] == 1
    assert results[0]['diagnostics']['errors'] == {'parse:JSONDecodeError': 1}
    assert results[1]['diagnostics']['event_kinds'] == {'quote': 1}
    assert results[1]['accepted'] == 1 and s.state.runtime['last_valid'] is not None
    assert f.state.runtime['last_valid'] is None
    assert not s.conflict
    f.close(); s.close()


@pytest.mark.parametrize('body,subscription,errors', [
    ('{"id":1,"code":2,"msg":"rejected"}', 'REJECTED', {'subscription:ValueError': 1}),
    ('{"code":2,"msg":"rejected"}', 'REJECTED', {'subscription:ValueError': 1}),
    ('{"id":1,"result":null}', 'ACKNOWLEDGED', {'receive:OSError': 1}),
])
def test_public_subscription_diagnostics(tmp_path, body, subscription, errors):
    account = RealtimeAccount(tmp_path / 's', Market.BINANCE_EQUITY)
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def send(self, message):
            assert json.loads(message)['method'] == 'SUBSCRIBE'
        async def recv(self):
            nonlocal body
            if body is None:
                raise OSError('ended')
            value, body = body, None
            return value
    result = asyncio.run(observe(account, seconds=1, network=True, reconnects=0,
        connector=lambda *_, **__: Socket()))
    diagnostic = result['diagnostics']
    assert diagnostic['handshakes'] == 1 and diagnostic['frames'] == 1
    assert diagnostic['subscription'] == subscription and diagnostic['errors'] == errors
    assert diagnostic['status'] == 'NO_MARKET_MESSAGES'
    assert result['messages'] == result['accepted'] == 0
    account.close()


@pytest.mark.parametrize('stale', [False, True])
def test_public_mark_diagnostics_and_time_rejection(tmp_path, stale):
    account = RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM)
    class Socket:
        sent = False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def recv(self):
            if self.sent:
                raise OSError('ended')
            self.sent = True
            ms = int((datetime.now(timezone.utc) - timedelta(seconds=30 if stale else 0)).timestamp() * 1000)
            return json.dumps(dict(data=dict(e='markPriceUpdate', s='NVDAUSDT', E=ms, p='100')))
    result = asyncio.run(observe(account, seconds=1, network=True, reconnects=0,
        connector=lambda *_, **__: Socket()))
    diagnostic = result['diagnostics']
    assert result['messages'] == diagnostic['parsed'] == 1
    assert diagnostic['event_kinds'] == {'mark': 1}
    assert result['accepted'] == int(not stale) and diagnostic['rejected'] == int(stale)
    assert diagnostic['rejection_reasons'] == ({'STALE_OR_FUTURE_OBSERVATION': 1} if stale else {})
    assert diagnostic['strategy_enabled'] is False
    assert account.state.runtime['signals'] == account.state.runtime['orders'] == 0
    assert account.state.quotes == {}
    account.close()


def test_public_handshake_without_frames(tmp_path):
    account = RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM)
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def recv(self):
            await asyncio.Future()
    result = asyncio.run(observe(account, seconds=.01, network=True, reconnects=0,
        connector=lambda *_, **__: Socket()))
    diagnostic = result['diagnostics']
    assert diagnostic['handshakes'] == diagnostic['receive_timeouts'] == 1
    assert diagnostic['frames'] == 0 and diagnostic['status'] == 'NO_MARKET_MESSAGES'
    account.close()


@pytest.mark.parametrize('market', list(Market))
def test_restart_invalidates_rules_and_session_even_within_ttl(tmp_path, market):
    h = Harness(tmp_path / 'a', market)
    h.quote('100')
    h.account.close()
    a = RealtimeAccount(tmp_path / 'a', market, clock=lambda: h.time)
    assert a.state.runtime['rules'] is None and a.state.runtime['session'] is None
    a.control('start')
    intent = OrderIntent(market, a.symbol, Side.BUY, OrderType.MARKET, D('.1'), None,
                         'manual000000000000000000000000000')
    with pytest.raises(Unavailable, match='UNKNOWN_RULES'):
        a.submit(intent)
    a.close()


@pytest.mark.parametrize('market', list(Market))
def test_source_string_and_serialized_evidence_cannot_authorize_public_orders(tmp_path, market):
    a = RealtimeAccount(tmp_path / 'a', market, clock=lambda: NOW, data_mode='public')
    a.event(dict(kind='rules', symbol=a.symbol, time=NOW, received=NOW, payload=rules(market),
                 source='https://fapi.binance.com/fapi/v1/exchangeInfo', reference='100',
                 evidence=dict(endpoint='https://fapi.binance.com', verified=True)))
    with pytest.raises(Unavailable, match='UNVERIFIED_RULE_PROVENANCE'):
        a._admit(OrderIntent(market, a.symbol, Side.BUY, OrderType.MARKET, D('.1'), None,
                            'manual000000000000000000000000000'))
    a.close()


def test_bounded_public_refresh_receipt_expiry_failure_and_restart(tmp_path):
    from factorlab.integrations.binance.client import Response
    from factorlab.errors import DataQualityError
    now = [NOW]
    calls = []
    raw = dict(serverTime=int(NOW.timestamp() * 1000), symbols=[rules(Market.BINANCE_USDM)['symbol']])
    def transport(url, timeout):
        calls.append((url, timeout))
        return Response(200, {}, json.dumps(raw).encode())
    a = RealtimeAccount(tmp_path / 'a', Market.BINANCE_USDM, clock=lambda: now[0], data_mode='public')
    assert a.refresh_rules(network=True, futures_transport=transport)
    receipt = a.state.runtime['rules']['evidence']
    assert receipt['raw'] == raw and receipt['timestamp_basis'] == 'LOCAL_FETCH'
    assert receipt['parsed']['LOT_SIZE']['stepSize'] == D('.01')
    assert receipt['parsed']['account_limits'] == 'UNKNOWN'
    assert set(a.state.runtime['watermarks']) == {'rules', 'session'}
    assert calls == [('https://fapi.binance.com/fapi/v1/exchangeInfo', 10)]
    a.event(dict(kind='mark', symbol=a.symbol, time=now[0], received=now[0], price='100'))
    intent = OrderIntent(a.market, a.symbol, Side.BUY, OrderType.MARKET, D('.1'), None, 'public-account-unknown')
    assert receipt['provenance'] == 'OFFLINE_INJECTED'
    with pytest.raises(Unavailable, match='UNVERIFIED_RULE_PROVENANCE'):
        a._admit(intent)
    now[0] += timedelta(seconds=301)
    assert a.observation_health()['rules']['validity'] == 'EXPIRED'
    with pytest.raises(Unavailable, match='UNVERIFIED_RULE_PROVENANCE'):
        a._admit(intent)
    assert a.refresh_rules(network=True, futures_transport=transport)
    def timeout(*_):
        raise TimeoutError('injected')
    with pytest.raises(DataQualityError):
        a.refresh_rules(network=True, futures_transport=timeout)
    assert a.state.runtime['rules'] is None
    now[0] += timedelta(seconds=1)
    assert a.refresh_rules(network=True, futures_transport=transport)
    a.close()
    a = RealtimeAccount(tmp_path / 'a', Market.BINANCE_USDM, clock=lambda: now[0], data_mode='public')
    assert a.observation_health()['rules']['validity'] == 'UNKNOWN'
    now[0] += timedelta(seconds=1)
    assert a.refresh_rules(network=True, futures_transport=transport)
    assert a.observation_health()['rules']['validity'] == 'UNVERIFIED'
    a.close()


def test_official_reader_path_and_stale_account_limits_gate(tmp_path, monkeypatch):
    from types import SimpleNamespace
    now = [NOW]
    raw = dict(symbols=[rules(Market.BINANCE_USDM)['symbol']])
    class Result:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps(raw).encode()
    def open_read(request, timeout):
        assert request.get_method() == 'GET'
        assert request.full_url == 'https://fapi.binance.com/fapi/v1/exchangeInfo'
        return Result()
    monkeypatch.setattr('factorlab.integrations.binance.client.build_opener',
                        lambda *_: SimpleNamespace(open=open_read))
    a = RealtimeAccount(tmp_path / 'a', Market.BINANCE_USDM, clock=lambda: now[0], data_mode='public')
    assert a.refresh_rules(network=True)
    assert a.observation_health()['rules']['validity'] == 'VERIFIED'
    a.event(dict(kind='mark', symbol=a.symbol, time=now[0], received=now[0], price='100'))
    intent = OrderIntent(a.market, a.symbol, Side.BUY, OrderType.MARKET, D('.1'), None, 'missing-account')
    with pytest.raises(Unavailable, match='UNKNOWN_ACCOUNT_RISK_BRACKETS'):
        a._admit(intent)
    now[0] += timedelta(seconds=301)
    with pytest.raises(Unavailable, match='STALE'):
        a._admit(intent)
    a.close()


def test_updated_offline_rules_change_admission_without_resetting_quotes(tmp_path):
    h = Harness(tmp_path / 'a')
    h.quote('100')
    h.time += timedelta(seconds=1)
    changed = rules(h.market)
    changed['symbol']['filters'][0]['stepSize'] = '0.3'
    h.send('rules', payload=changed, source='SYNTHETIC_OFFLINE_ONLY')
    intent = OrderIntent(h.market, h.account.symbol, Side.BUY, OrderType.MARKET, D('.1'), None, 'updated-rule')
    with pytest.raises(Unavailable, match='QUANTITY_FILTER'):
        h.account.submit(intent)
    h.time += timedelta(seconds=1)
    h.send('rules', payload=rules(h.market), source='SYNTHETIC_OFFLINE_ONLY')
    assert h.account.submit(intent).client_order_id == intent.client_order_id
    h.account.close()
