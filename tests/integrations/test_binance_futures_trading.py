from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import pytest

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.client import Response, PublicClient
from factorlab.integrations.binance_futures_trading.account import AccountFacts
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.demo import run
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW, MS, synthetic_rules
from factorlab.integrations.binance_futures_trading.protocol import (
    Credentials, ExchangeError, FuturesClient, OfflineTransport, OutcomeUnknown,
    ReadOnlyNetworkTransport, SafetyBlocked,
)
from factorlab.integrations.binance_futures_trading.rules import order_parameters, current_tier, observe_rules
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from factorlab.trading import Market, OrderIntent, OrderType, OrderStatus as S, Side
from factorlab.trading.storage import PaperStore


def intent(key='one', side=Side.BUY, reduce=False, limit=None, quantity='2'):
    return OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', side,
                       OrderType.MARKET if limit is None else OrderType.LIMIT, D(quantity),
                       None if limit is None else D(limit), key, reduce_only=reduce)


def setup(path=':memory:', transport=None):
    transport = FakeTransport() if transport is None else transport
    client = FuturesClient(Credentials('test-key', 'test-secret'), transport=transport, clock=lambda: NOW.timestamp())
    store = PaperStore(path)
    adapter = FuturesAdapter(client, store, clock=lambda: NOW)
    adapter.reconcile()
    return adapter, transport


@pytest.mark.parametrize('limit', [None, '100'])
def test_order_fields_and_exact_signature(limit):
    fake = FakeTransport()
    client = FuturesClient(Credentials('key', 'secret'), transport=fake, clock=lambda: 1700000000)
    params = order_parameters(intent(limit=limit))
    request = client.build('POST', '/fapi/v1/order', params)
    unsigned, signature = request.query.rsplit('&signature=', 1)
    assert signature == hmac.new(b'secret', unsigned.encode(), hashlib.sha256).hexdigest()
    values = parse_qs(unsigned)
    assert values['timestamp'] == ['1700000000000']
    assert values['recvWindow'] == ['5000']
    assert values['quantity'] == ['2']
    assert values['positionSide'] == ['BOTH']
    assert values['reduceOnly'] == ['false']
    assert ('timeInForce' in values) == (limit is not None)
    assert ('price' in values) == (limit is not None)
    assert request.headers['X-MBX-APIKEY'] == 'key'
    assert 'secret' not in repr(client.credentials)
    assert 'signature' not in repr(request) and 'key' not in repr(request)


def test_first_confirmation_uses_exchange_time_not_local_clock():
    fake = FakeTransport()
    client = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp() + 2)
    client.sync_time()
    a = FuturesAdapter(client, PaperStore(':memory:'), clock=lambda: NOW + timedelta(seconds=2))
    a.reconcile()
    assert a.submit_order(intent()).event_time == NOW


def test_explicit_read_transport_builds_only_authenticated_get(monkeypatch):
    from types import SimpleNamespace
    calls = []
    class Result:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return b'{"assets":[],"positions":[]}'
    def open_read(request, timeout):
        calls.append(request)
        return Result()
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener',
                        lambda *args: SimpleNamespace(open=open_read))
    client = FuturesClient(Credentials('read-key', 'read-secret'),
        transport=ReadOnlyNetworkTransport(permit_account_read=True), clock=lambda: NOW.timestamp())
    assert client.request('GET', '/fapi/v3/account') == {'assets': [], 'positions': []}
    assert calls[0].get_method() == 'GET'
    assert calls[0].full_url.startswith('https://fapi.binance.com/fapi/v3/account?')
    assert calls[0].get_header('X-mbx-apikey') == 'read-key'
    assert 'read-secret' not in calls[0].full_url


def test_credentials_environment_and_server_clock(monkeypatch):
    monkeypatch.setenv('BINANCE_API_KEY', 'env-key')
    monkeypatch.setenv('BINANCE_API_SECRET', 'env-secret')
    client = FuturesClient(Credentials.from_env(), transport=FakeTransport(), clock=lambda: NOW.timestamp() - 2)
    assert client.sync_time() == 2000
    request = client.build('GET', '/fapi/v3/account')
    assert parse_qs(request.query)['timestamp'] == [str(MS)]
    monkeypatch.delenv('BINANCE_API_SECRET')
    with pytest.raises(ValueError):
        Credentials.from_env()


@pytest.mark.parametrize('side,reduce,expected', [
    (Side.BUY, False, 'BUY'), (Side.SELL, False, 'SELL'),
    (Side.SELL, True, 'SELL'), (Side.BUY, True, 'BUY')])
def test_long_short_close_mapping(side, reduce, expected):
    p = order_parameters(intent(side=side, reduce=reduce))
    assert p['side'] == expected and p['reduceOnly'] == str(reduce).lower()


@pytest.mark.parametrize('hedge_side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('side', list(Side))
def test_hedge_protocol_distinguishes_direction_without_reduce_only(hedge_side, side):
    p = order_parameters(intent(side=side), hedge_mode=True, hedge_side=hedge_side)
    assert p['positionSide'] == hedge_side and p['side'] == side.value
    assert 'reduceOnly' not in p
    with pytest.raises(ValueError, match='forbids'):
        order_parameters(intent(reduce=True), hedge_mode=True, hedge_side=hedge_side)
    with pytest.raises(ValueError):
        order_parameters(intent(), hedge_side=hedge_side)


def test_hedge_and_missing_account_fields_cannot_claim_reconciliation():
    a, fake = setup()
    fake.hedge_mode = True
    with pytest.raises(DataQualityError, match='hedge'):
        a.reconcile()
    facts = AccountFacts('x', NOW, ({'asset': 'USDT'},), (), (), (), (), (), None)
    assert facts.usdt()['availableBalance'] is None
    assert not facts.shared(reconciled=True).reconciled
    assert not facts.shared().assets


def test_partial_duplicate_cancel_and_real_cumulative_mapping():
    a, fake = setup()
    a.submit_order(intent(limit='100'))
    stream = UserDataEvents(a)
    msg = fake.trade_update('one', '0.5', '99')
    event = stream.parse(msg)
    assert stream.parse(msg) == event
    assert event.fill_price == D('99') and event.cumulative_fee == D('0.0495')
    assert a.order('one').cumulative_filled_quantity == D('0.5')
    canceled = a.cancel_order('one')
    assert canceled.status == S.CANCELED and canceled.cumulative_filled_quantity == D('0.5')
    # REST cumulative quote is converted into incremental fill price.
    fake.trade_update('one', '1.5', '100')
    full = a.query_order('one')
    assert full.status == S.FILLED and full.fill_price == D('100')
    assert full.cumulative_filled_quantity == D('2')
    assert full.cumulative_fee == D('0.1995')
    assert a.query_order('one') == full
    assert len(a.all_orders('NVDAUSDT')) == 1
    assert not a.open_orders()


def test_order_idempotency_and_identity_conflict():
    a, fake = setup()
    first = a.submit_order(intent())
    assert a.submit_order(intent()) == first
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1
    with pytest.raises(ValueError, match='conflict'):
        a.submit_order(intent(quantity='3'))


def test_timeout_unknown_restart_reconcile_no_duplicate_post(tmp_path):
    path = tmp_path / 'adapter.sqlite'
    a, fake = setup(path)
    fake.timeout_next = True
    with pytest.raises(OutcomeUnknown):
        a.submit_order(intent())
    assert a.order('one').status == S.UNKNOWN and not a.reconciled
    with pytest.raises(OutcomeUnknown):
        a.submit_order(intent())
    a.store.close()
    client = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp())
    recovered = FuturesAdapter(client, PaperStore(path), clock=lambda: NOW)
    assert not recovered.reconciled
    with pytest.raises(SafetyBlocked):
        recovered.submit_order(intent('replacement'))
    assert recovered.reconcile().reconciled
    assert recovered.submit_order(intent()).status == S.SUBMITTED
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1


def test_unknown_absent_order_does_not_allow_replacement():
    class Missing(FakeTransport):
        def __call__(self, request, timeout):
            if request.method == 'POST':
                raise TimeoutError
            return super().__call__(request, timeout)
    a, _ = setup(transport=Missing())
    with pytest.raises(OutcomeUnknown):
        a.submit_order(intent())
    with pytest.raises(ExchangeError):
        a.reconcile()
    assert not a.reconciled and a.order('one').reconcile_required
    with pytest.raises(SafetyBlocked):
        a.submit_order(intent('replacement'))


def test_out_of_order_gap_and_reconnect_require_account_read():
    a, fake = setup()
    a.submit_order(intent())
    stream = UserDataEvents(a)
    first = fake.trade_update('one', '0.5')
    second = fake.trade_update('one', '0.5')
    with pytest.raises(DataQualityError, match='reconciliation'):
        stream.parse(second)  # cumulative jump, missing first trade
    assert not a.reconciled
    assert a.reconcile().reconciled
    assert a.order('one').cumulative_filled_quantity == D('1')
    with pytest.raises(DataQualityError):
        stream.parse(first)
    stream.disconnected()
    assert not a.reconciled
    assert a.reconcile().reconciled
    assert a.read_account().positions[0].quantity == D('1')


def test_account_update_is_delta_not_public_funding_or_entire_snapshot():
    a, _ = setup()
    stream = UserDataEvents(a)
    delta = stream.parse({'e': 'ACCOUNT_UPDATE', 'E': MS, 'T': MS,
                          'a': {'m': 'FUNDING_FEE', 'B': [{'a': 'USDT', 'bc': '-0.02'}], 'P': []}})
    assert delta['reconcile_required'] and not a.reconciled
    assert a.income()[0]['incomeType'] == 'FUNDING_FEE'
    a.reconcile()
    facts = replace(a.facts, income=a.income())
    assert facts.funding_payments()[0]['income'] == '-0.02'
    with pytest.raises(DataQualityError):
        stream.parse({'e': 'markPriceUpdate', 'r': '0.001'})
    assert not a.reconciled


@pytest.mark.parametrize('change,expected', [
    ({'quantity': D('0.015')}, 'QUANTITY_FILTER'),
    ({'quantity': D('0.01')}, 'MIN_NOTIONAL'),
    ({'limit_price': D('100.001'), 'order_type': OrderType.LIMIT}, 'PRICE_FILTER'),
])
def test_precise_filters(change, expected):
    result = synthetic_rules().validate(replace(intent(), **change), mark_price=D('100'), leverage=1,
        available_margin=D('1000'), current_notional=D('0'), now=NOW)
    assert not result.approved and result.reason == expected


@pytest.mark.parametrize('kwargs,expected', [
    ({'leverage': 2}, 'LEVERAGE_LIMIT'),
    ({'available_margin': D('1')}, 'INSUFFICIENT_MARGIN'),
    ({'available_margin': None}, 'UNKNOWN_ACCOUNT_OR_MARK'),
    ({'current_notional': D('9900')}, 'RISK_BRACKET_LIMIT'),
    ({'now': NOW + timedelta(seconds=301)}, 'STALE_RULES'),
])
def test_perpetual_risk_fail_closed(kwargs, expected):
    params = dict(mark_price=D('100'), leverage=1, available_margin=D('1000'), current_notional=D('0'), now=NOW)
    params.update(kwargs)
    assert synthetic_rules().validate(intent(), **params).reason == expected
    assert current_tier(synthetic_rules(), D('200'))['bracket'] == 1
    assert current_tier(synthetic_rules(), None) is None


@pytest.mark.parametrize('status,code,category', [(400, -2019, 'INSUFFICIENT_MARGIN'),
    (400, -1021, 'CLOCK_SKEW'), (401, -2015, 'AUTH'), (400, -2022, 'REDUCE_ONLY')])
def test_exchange_error_mapping_without_sensitive_messages(status, code, category):
    fake = FakeTransport()
    client = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp())
    fake.response_next = Response(status, {}, json.dumps({'code': code, 'msg': 'sensitive'}).encode())
    with pytest.raises(ExchangeError) as caught:
        client.request('GET', '/fapi/v3/account')
    assert caught.value.category == category and 'sensitive' not in str(caught.value)
    assert len(fake.calls) == 1


def test_rate_limit_retry_after_blocks_without_sleep_or_resend():
    fake = FakeTransport()
    client = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp())
    fake.response_next = Response(429, {'Retry-After': '12'}, b'{"code":-1003}')
    with pytest.raises(ExchangeError) as caught:
        client.request('GET', '/fapi/v3/account')
    assert caught.value.retry_after == 12
    with pytest.raises(ExchangeError):
        client.request('GET', '/fapi/v3/account')
    assert len(fake.calls) == 1


@pytest.mark.parametrize('response', [Response(503, {}, b'{"code":-1007}'), Response(408, {}, b'{}'),
    Response(400, {}, b'{"code":-4116}'), Response(200, {}, b'not-json'), Response(200, {}, b'42')])
def test_uncertain_write_responses_never_retry(response):
    a, fake = setup()
    fake.response_next = response
    with pytest.raises(OutcomeUnknown):
        a.submit_order(intent())
    assert a.order('one').status == S.UNKNOWN
    assert len([c for c in fake.calls if c[0] == 'POST']) == 1


def test_real_network_and_mutating_configuration_are_blocked(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('network must not execute')
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener', forbidden)
    credentials = Credentials('k', 's')
    with pytest.raises(SafetyBlocked):
        FuturesClient(credentials).request('GET', '/fapi/v3/account')
    client = FuturesClient(credentials, transport=ReadOnlyNetworkTransport())
    with pytest.raises(SafetyBlocked):
        client.request('GET', '/fapi/v3/account')
    permitted = FuturesClient(credentials, transport=ReadOnlyNetworkTransport(permit_account_read=True))
    for method, path in [('POST', '/fapi/v1/order'), ('DELETE', '/fapi/v1/order'),
                         ('POST', '/fapi/v1/leverage'), ('POST', '/fapi/v1/marginType'), ('POST', '/sapi/v1/asset/transfer')]:
        with pytest.raises(SafetyBlocked):
            permitted.request(method, path)
    # Network transport independently refuses writes even without the client.
    with pytest.raises(SafetyBlocked):
        permitted.transport(permitted.build('DELETE', '/fapi/v1/order'), 1)


def test_incomplete_actual_trades_cannot_invent_zero_fee():
    a, fake = setup()
    a.submit_order(intent())
    fake.trade_update('one')
    fake.trades.clear()
    with pytest.raises(DataQualityError, match='fee UNKNOWN'):
        a.query_order('one')
    assert a.order('one').reconcile_required


def test_gateway_end_to_end(tmp_path):
    result = run(tmp_path)
    assert result['reconciled'] and result['status'] == 'FILLED'
    assert D(result['quantity']) == D('2')
    assert D(result['wallet_USDT']) == D('999.8')
    assert D(result['collateral_USDT']) == D('200')
    assert D(result['fees_USDT']) == D('0.2')


def gateway_setup(adapter, paper_path=':memory:', rules=None):
    from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
    from factorlab.trading.control import RiskLimits, TradingController
    from factorlab.trading.paper import Instrument, Quote
    gateway = FuturesPaperGateway(adapter, synthetic_rules() if rules is None else rules,
                                  (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('1000')})
    controller = TradingController(gateway, PaperStore(paper_path), clock=lambda: NOW,
                                  limits=RiskLimits(D('500'), D('500'), D('800'), 10))
    controller.quote(Quote('NVDAUSDT', D('100'), NOW))
    return gateway, controller


def test_gateway_restart_recovers_confirmed_missed_fills_not_replacement(tmp_path):
    protocol_path, paper_path = tmp_path / 'protocol.sqlite', tmp_path / 'paper.sqlite'
    a, fake = setup(protocol_path)
    gateway, c = gateway_setup(a, paper_path)
    c.submit(intent())
    message = fake.trade_update('one', '0.5')
    c.replay(UserDataEvents(a).parse(message))
    fake.trade_update('one', '1.5')  # disconnected; no local confirmation
    a.disconnect()
    a.store.close()
    c.store.close()
    client = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp())
    restored = FuturesAdapter(client, PaperStore(protocol_path), clock=lambda: NOW)
    gateway, c = gateway_setup(restored, paper_path)
    assert not c.account().reconciled
    with pytest.raises(ValueError, match='RECONCILE_REQUIRED'):
        c.submit(intent())
    account = gateway.recover(c)
    assert account.reconciled and account.positions[0].quantity == D('2')
    assert account.assets[0].total == D('999.8')
    assert gateway.recover(c).assets == account.assets
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_gateway_rejects_margin_bracket_and_shared_risk_before_post():
    from factorlab.trading.control import RiskRejected
    a, fake = setup()
    gateway, c = gateway_setup(a)
    a.facts = replace(a.facts, assets=({'asset': 'USDT', 'walletBalance': '1000', 'availableBalance': '1'},))
    with pytest.raises(RiskRejected, match='INSUFFICIENT_MARGIN'):
        c.submit(intent())
    a.reconcile()
    gateway.rules = replace(synthetic_rules(), brackets=({'notionalFloor': '0', 'notionalCap': '100', 'initialLeverage': 1},))
    with pytest.raises(RiskRejected, match='RISK_BRACKET_LIMIT'):
        c.submit(intent())
    gateway.rules = synthetic_rules()
    c.set_kill_switch()
    with pytest.raises(RiskRejected, match='KILL_SWITCH'):
        c.submit(intent())
    assert not [call for call in fake.calls if call[0] == 'POST']


def test_observe_rules_reuses_existing_public_client():
    rule = synthetic_rules()
    info = {'symbols': [{'symbol': 'NVDAUSDT', 'status': 'TRADING', 'quoteAsset': 'USDT',
                        'marginAsset': 'USDT', 'filters': list(rule.filters.values())}]}
    public = PublicClient(transport=lambda url, timeout: Response(200, {}, json.dumps(info).encode()), retries=0)
    fake = FakeTransport()
    signed = FuturesClient(Credentials('k', 's'), transport=fake, clock=lambda: NOW.timestamp())
    observation = observe_rules(public, signed, 'NVDAUSDT', NOW, 'synthetic-test')
    assert observation.symbol == 'NVDAUSDT' and observation.source_version == 'synthetic-test'
    assert public.attempts == 1


def test_result_response_may_already_be_filled():
    class ImmediateFill(FakeTransport):
        def __call__(self, request, timeout):
            result = super().__call__(request, timeout)
            if request.method == 'POST':
                self.trade_update('one', '2')
                return Response(200, {}, json.dumps(self.orders['one']).encode())
            return result
    a, _ = setup(transport=ImmediateFill())
    assert a.submit_order(intent()).status == S.FILLED
    assert a.order('one').cumulative_fee == D('0.2')


def test_gateway_unknown_submit_recovers_confirmation_without_repost():
    a, fake = setup()
    gateway, c = gateway_setup(a)
    fake.timeout_next = True
    assert c.submit(intent()) is None
    assert c.account().open_orders[0].status == S.UNKNOWN
    assert not c.account().reconciled
    account = gateway.recover(c)
    assert account.reconciled and account.open_orders[0].status == S.SUBMITTED
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_gateway_orphan_commit_recovers_existing_identity_without_post():
    a, fake = setup()
    # Protocol accepted an order before a crash rolled back paper projection.
    a.submit_order(intent())
    fake.trade_update('one', '2')
    gateway, c = gateway_setup(a)
    with pytest.raises(SafetyBlocked, match='identities differ'):
        c.submit(intent('new'))
    account = gateway.recover(c)
    assert account.reconciled and account.positions[0].quantity == D('2')
    assert account.assets[0].total == D('999.8')
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_cancel_timeout_does_not_erase_competing_fill():
    class LostCancel(FakeTransport):
        def __call__(self, request, timeout):
            if request.method == 'DELETE':
                self.trade_update('one', '2')
                raise TimeoutError
            return super().__call__(request, timeout)
    a, fake = setup(transport=LostCancel())
    a.submit_order(intent())
    with pytest.raises(OutcomeUnknown):
        a.cancel_order('one')
    assert a.order('one').status == S.UNKNOWN
    assert a.reconcile().reconciled
    assert a.order('one').status == S.FILLED
    assert a.read_account().positions[0].quantity == D('2')
    assert len([call for call in fake.calls if call[0] == 'POST']) == 1


def test_actual_account_fields_keep_unknown_and_report_only_exchange_liquidation():
    a, fake = setup()
    a.submit_order(intent())
    fake.trade_update('one')
    a.reconcile()
    facts = a.read_activity('NVDAUSDT')
    p = facts.position_details()[0]
    assert p['quantity'] == D('1') and p['entry_price'] == D('100')
    assert p['leverage'] == 1 and p['margin_type'] == 'CROSSED'
    assert p['liquidation_price'] == D('0')
    assert facts.usdt()['marginBalance'] == D('999.9')
    assert len(facts.trades) == 1 and len(facts.funding_payments()) == 1
    missing = replace(facts, risk=({},), symbol_config=())
    assert missing.position_details()[0]['liquidation_price'] is None
    assert missing.position_details()[0]['leverage'] is None
