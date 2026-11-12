"""Synthetic deterministic scenarios are not online market/session evidence."""
from datetime import timedelta
from decimal import Decimal as D
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from factorlab.trading import Market, OrderIntent, OrderType, OrderStatus, Side
from factorlab.trading.realtime import RealtimeAccount, PersistenceConflict, equity_events
from factorlab.trading.simulation import PAPER_SIMULATION
from factorlab.integrations.binance_equity_trading.protocol import Unavailable
from test_realtime import Harness, NOW, rules


@pytest.mark.parametrize('market', list(Market))
def test_simulation_partial_cancel_duplicate_restart_and_accounting(tmp_path, market):
    path = tmp_path / 'paper.sqlite'
    h = Harness(path, market, simulation_policy=PAPER_SIMULATION, acceptance_signal=True)
    a = h.account
    if market == Market.BINANCE_USDM:
        payload = rules(market)
        payload['bracket'] = None
        h.time += timedelta(milliseconds=1)
        h.send('rules', payload=payload, reference=None, source='SYNTHETIC_OFFLINE_ONLY')
    h.quote('100')
    h.time += timedelta(seconds=30)
    h.quote('100')
    assert a.health()['signals'] == a.health()['orders'] == 1
    assert a.health()['signal_source'].startswith('DETERMINISTIC_PAPER_ACCEPTANCE')
    assert a.health()['risk']['simulated'] and a.health()['risk']['approved']
    assert a.health()['account']['simulated']
    assert not a.state.positions
    event = h.quote('99', '.4')
    assert a.health()['filled_quantity'] == D('.04')
    assert a.health()['slippage_cost'] == D('.000396')
    assert a.health()['fees'] == D('.003960396')
    order_id = next(iter(a.state.orders))
    assert a.state.orders[order_id].snapshot.status == OrderStatus.PARTIALLY_FILLED
    before = a.controller.account()
    assert not a.event(event)
    assert not a.replay(a.state.confirmations[order_id])
    assert a.controller.account() == before
    a.cancel(order_id)
    assert a.state.orders[order_id].snapshot.status == OrderStatus.CANCELED
    before = a.controller.account()
    assert a.store.load() == a.state
    a.close()
    restored = RealtimeAccount(path, market, clock=lambda: h.time,
        simulation_policy=PAPER_SIMULATION, acceptance_signal=True)
    assert restored.controller.account() == before
    assert restored.state.orders[order_id].snapshot.status == OrderStatus.CANCELED
    assert restored.health()['filled_quantity'] == D('.04')
    assert restored.health()['simulated']
    currency = 'USDT' if market == Market.BINANCE_USDM else 'USD'
    assert set(restored.state.cash) == {currency}
    expected = D('1000') - D('.003960396')
    if market == Market.BINANCE_EQUITY:
        expected -= D('.04') * D('99.0099')
    assert restored.state.cash[currency] == expected
    assert restored.state.positions[restored.symbol].quantity == D('.04')
    assert restored.state.positions[restored.symbol].entry_price == D('99.0099')
    assert restored.state.positions[restored.symbol].mark_price == D('99')
    assert restored.health()['simulation_policy']['settlement'] == 'UNKNOWN'
    restored.close()
    code = '''import sys
from datetime import datetime
from factorlab.trading import Market
from factorlab.trading.realtime import RealtimeAccount
from factorlab.trading.simulation import PAPER_SIMULATION
from factorlab.trading.storage import dumps
account = RealtimeAccount(sys.argv[1], Market(sys.argv[2]), clock=lambda: datetime.fromisoformat(sys.argv[3]),
    simulation_policy=PAPER_SIMULATION, acceptance_signal=True)
print(dumps(account.controller.account()))
print(next(iter(account.state.orders)))
account.close()
'''
    result = subprocess.run([sys.executable, '-c', code, str(path), market.value, h.time.isoformat()],
        capture_output=True, text=True, check=True, timeout=30)
    from factorlab.trading.storage import dumps
    snapshot, restored_id = result.stdout.splitlines()
    assert snapshot == dumps(before) and restored_id == order_id
    with pytest.raises(PersistenceConflict, match='configuration changed'):
        RealtimeAccount(path, market, clock=lambda: h.time)


@pytest.mark.parametrize('market', list(Market))
def test_public_simulation_uses_official_reader_and_session_not_forged_state(tmp_path, monkeypatch, market):
    now = [NOW]
    row = rules(market)
    raw = dict(symbols=[row['symbol'] if market == Market.BINANCE_USDM else row])
    class Response:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return json.dumps(raw).encode()
    def open_read(request, timeout):
        assert request.get_method() == 'GET'
        return Response()
    reader = ('factorlab.integrations.binance.client' if market == Market.BINANCE_USDM
              else 'factorlab.integrations.binance_equity.client')
    monkeypatch.setattr(reader + '.build_opener', lambda *_: SimpleNamespace(open=open_read))
    if market == Market.BINANCE_EQUITY:
        monkeypatch.setenv('BINANCE_EQUITY_API_KEY', 'offlinefixturekey')
    a = RealtimeAccount(tmp_path / 'a', market, clock=lambda: now[0], data_mode='public',
                        simulation_policy=PAPER_SIMULATION)
    a.refresh_rules(network=True)
    assert a.observation_health()['rules']['validity'] == 'VERIFIED'
    if market == Market.BINANCE_EQUITY:
        wire = dict(e='price', rates=[dict(s='NVDA', mp='O', t=None)])
        a._stream_event(next(equity_events(wire, now[0])), wire, official_transport=True)
    else:
        assert a.state.runtime['rules']['payload']['bracket'] is None
        assert a.state.runtime['rules']['evidence']['parsed']['account_limits'] == 'UNKNOWN'
        a.event(dict(kind='mark', symbol=a.symbol, time=now[0], received=now[0], price='100'))
    a.control('start')
    kind = dict(kind='trade', price='100', size='1') if market == Market.BINANCE_USDM else dict(
        kind='quote', ask='100', bid='99.99', ask_size='1', bid_size='1')
    a.event(dict(symbol=a.symbol, time=now[0], received=now[0], **kind))
    intent = OrderIntent(market, a.symbol, Side.BUY, OrderType.LIMIT, D('.1'), D('100'), 'p' * 32)
    a.submit(intent)
    now[0] += timedelta(seconds=1)
    kind.update(price='99') if market == Market.BINANCE_USDM else kind.update(ask='99', bid='98.99')
    a.event(dict(symbol=a.symbol, time=now[0], received=now[0], **kind))
    assert a.health()['fills'] == 1
    assert a.state.positions[a.symbol].quantity == D('.1')
    if market == Market.BINANCE_USDM:
        assert a.state.positions[a.symbol].mark_price == D('100')
    now[0] += timedelta(seconds=301)
    with pytest.raises(Unavailable, match='STALE'):
        a._admit(intent)
    a.close()
    restored = RealtimeAccount(tmp_path / 'a', market, clock=lambda: now[0], data_mode='public',
                               simulation_policy=PAPER_SIMULATION)
    assert restored.observation_health()['rules']['validity'] == 'UNKNOWN'
    with pytest.raises(Unavailable, match='UNKNOWN_RULES'):
        restored._admit(intent)
    restored.close()


@pytest.mark.parametrize('market', list(Market))
def test_policy_cannot_bypass_forged_public_provenance(tmp_path, market):
    a = RealtimeAccount(tmp_path / 'a', market, clock=lambda: NOW, data_mode='public',
                        simulation_policy=PAPER_SIMULATION)
    a.event(dict(kind='rules', symbol=a.symbol, time=NOW, received=NOW, payload=rules(market),
        source='OFFICIAL', reference='100', evidence=dict(provenance='OFFICIAL_TRANSPORT', parsed=dict(validity='VALID'))))
    intent = OrderIntent(market, a.symbol, Side.BUY, OrderType.MARKET, D('.1'), None, 'p' * 32)
    with pytest.raises(Unavailable, match='UNVERIFIED_RULE_PROVENANCE'):
        a._admit(intent)
    a.close()


def test_simulation_unknown_and_stream_isolation(tmp_path):
    f = Harness(tmp_path / 'f', simulation_policy=PAPER_SIMULATION)
    s = Harness(tmp_path / 's', Market.BINANCE_EQUITY, simulation_policy=PAPER_SIMULATION)
    f.quote('100'); f.quote('101')
    key = next(iter(f.account.state.orders))
    def unknown():
        f.account.controller.broker.mark_unknown(key)
        f.account.state.confirmations.pop(key)
    f.account._change(unknown)
    intent = f.account.state.orders[key].snapshot.intent
    f.account.close()
    a = RealtimeAccount(tmp_path / 'f', Market.BINANCE_USDM, clock=lambda: f.time,
                        simulation_policy=PAPER_SIMULATION)
    a.control('start')
    with pytest.raises(Unavailable, match='QUERY_ONLY'):
        a.submit(intent)
    assert a.health()['orders'] == 1 and not a.state.reconciled
    a.disconnect('DISCONNECTED')
    s.quote('100'); s.quote('101'); s.quote('100')
    assert s.account.health()['fills'] == 1
    assert a.health()['fills'] == 0 and set(a.state.cash) == {'USDT'}
    assert set(s.account.state.cash) == {'USD'}
    a.close(); s.account.close()


@pytest.mark.parametrize('phase', ['CLOSED', 'UNKNOWN'])
def test_simulated_permission_does_not_open_a_closed_or_unknown_session(tmp_path, phase):
    h = Harness(tmp_path / 'a', Market.BINANCE_EQUITY, simulation_policy=PAPER_SIMULATION)
    h.time += timedelta(seconds=1)
    h.send('session', phase=phase)
    h.quote('100'); h.quote('101')
    assert h.account.health()['orders'] == 0
    assert h.account.health()['last_rejection'] == 'SESSION_NOT_TRADABLE'
    h.account.close()


@pytest.mark.parametrize('market', list(Market))
def test_simulation_no_touch_no_fill_and_stale_market_pauses(tmp_path, market):
    h = Harness(tmp_path / 'a', market, simulation_policy=PAPER_SIMULATION)
    h.quote('100')
    intent = OrderIntent(market, h.account.symbol, Side.BUY, OrderType.LIMIT, D('.1'), D('99'), 'n' * 32)
    h.account.submit(intent)
    h.quote('100')
    assert h.account.health()['fills'] == 0
    assert not h.account.state.positions
    h.time += timedelta(seconds=6)
    h.account.tick()
    assert h.account.health()['reason'] == 'STALE_QUOTE'
    with pytest.raises(Unavailable, match='RUNNER_NOT_RUNNING'):
        h.account.submit(OrderIntent(market, h.account.symbol, Side.BUY, OrderType.MARKET,
            D('.1'), None, 'new-risk'))
    h.account.close()


def test_simulation_cli_requires_explicit_flags_and_bounds(monkeypatch):
    from factorlab.trading.realtime_service import main
    for arguments in (['--paper-simulation'], ['--acceptance-signal'],
        ['--paper', '--paper-simulation', '--seconds', '61'],
        ['--paper', '--paper-simulation', '--reconnects', '3']):
        monkeypatch.setattr(sys, 'argv', ['paper', '--state-dir', 'UNUSED', *arguments])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2


def test_simulation_policy_and_signal_are_explicit(tmp_path):
    with pytest.raises(ValueError, match='explicit PAPER_SIMULATION'):
        RealtimeAccount(tmp_path / 'a', Market.BINANCE_USDM, acceptance_signal=True)
    with pytest.raises(ValueError, match='only PAPER_SIMULATION'):
        RealtimeAccount(tmp_path / 'b', Market.BINANCE_USDM, simulation_policy='LIVE_EXECUTION')


@pytest.mark.parametrize('method,path', [('POST', '/fapi/v1/order'), ('DELETE', '/fapi/v1/order'),
    ('POST', '/fapi/v1/leverage'), ('POST', '/fapi/v1/marginType'),
    ('POST', '/fapi/v1/positionSide/dual'), ('POST', '/sapi/v1/asset/transfer'),
    ('POST', '/sapi/v1/capital/withdraw/apply')])
def test_simulation_state_and_environment_do_not_authorize_real_network(tmp_path, monkeypatch, method, path):
    from factorlab.integrations.binance_futures_trading.protocol import (
        Credentials, Environment, FuturesClient, ReadOnlyNetworkTransport, SafetyBlocked)
    from factorlab.integrations.binance_equity_trading.requests import live_submit, live_cancel
    for name in ('PAPER_SIMULATION', 'LIVE_EXECUTION', 'BINANCE_LIVE_TRADING', 'ENABLE_LIVE_TRADING'):
        monkeypatch.setenv(name, 'true')
    h = Harness(tmp_path / 'a', simulation_policy=PAPER_SIMULATION)
    h.account._change(lambda: h.account.state.runtime.update(live_execution=True,
        simulation_policy=PAPER_SIMULATION.describe()))
    state = h.account.store.load()
    def forbidden(*args, **kwargs):
        pytest.fail('live network must remain unreachable')
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.protocol.build_opener', forbidden)
    client = FuturesClient(Credentials('offline-key', 'offline-secret', Environment.PRODUCTION),
        environment=Environment.PRODUCTION,
        transport=ReadOnlyNetworkTransport(environment=Environment.PRODUCTION, permit_account_read=True))
    with pytest.raises(SafetyBlocked, match='NO LIVE MONEY: all real trading requests forbidden'):
        client.request(method, path, state.runtime)
    for operation in (live_submit, live_cancel):
        with pytest.raises(PermissionError, match='NO LIVE MONEY'):
            operation(state.runtime)
    h.account.close()
