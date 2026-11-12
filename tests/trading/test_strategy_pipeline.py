import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.contracts import Market, OrderStatus
from factorlab.trading.control import RiskLimits
from factorlab.trading.realtime import RealtimeAccount, PersistenceConflict
from factorlab.trading.strategy import ClosedBar, DualMAStrategy
from factorlab.integrations.binance_futures_trading.market import closed_kline

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
RULES = dict(symbol=dict(symbol='NVDAUSDT', status='TRADING', quoteAsset='USDT', marginAsset='USDT',
    orderTypes=['MARKET', 'LIMIT'], filters=[
        dict(filterType='LOT_SIZE', minQty='.01', maxQty='100', stepSize='.01'),
        dict(filterType='MARKET_LOT_SIZE', minQty='.01', maxQty='100', stepSize='.01'),
        dict(filterType='PRICE_FILTER', minPrice='.01', maxPrice='10000', tickSize='.01'),
        dict(filterType='MIN_NOTIONAL', notional='5')]),
    bracket=dict(symbol='NVDAUSDT', brackets=[dict(notionalFloor='0', notionalCap='10000', initialLeverage=1)]))


class Runner:
    def __init__(self, path, *, limits=None, weight=D('.05')):
        self.now = START
        self.path = path
        self.strategy = DualMAStrategy(max_weight=weight)
        self.account = RealtimeAccount(path, Market.BINANCE_USDM, clock=lambda: self.now,
            strategy=self.strategy, limits=limits, participation=D(1), slippage=D(0))
        self.account.control('start')

    def send(self, kind, **fields):
        return self.account.event(dict(kind=kind, symbol='NVDAUSDT', time=self.now,
            received=self.now, **fields))

    def trade(self, price='110', size='10'):
        self.now += timedelta(milliseconds=1)
        self.send('rules', payload=RULES, source='SYNTHETIC_RULES', reference=price)
        self.send('mark', price=price)
        return self.send('trade', price=price, size=size)

    def bar(self, i, price):
        opened = START + timedelta(hours=i)
        closed = opened + timedelta(hours=1)
        self.now = closed
        p = D(price)
        bar = ClosedBar('NVDAUSDT', opened, closed, p, p, p, p, D(100), 3600,
                        'BINANCE_USDM_PUBLIC_REST')
        self.account.closed_bar(bar)
        return bar

    def cross(self):
        for i in range(20):
            self.bar(i, '100')
        return self.bar(20, '110')


def test_dual_ma_real_broker_fills_restarts_and_no_duplicate_economics(tmp_path):
    h = Runner(tmp_path / 'paper.sqlite')
    bar = h.cross()
    a = h.account
    assert a.state.runtime['pending_strategy_signal']['direction'] == 'BUY'
    assert not a.state.orders
    assert not a.closed_bar(bar)
    h.trade()
    assert len(a.state.orders) == 1 and not a.state.positions
    order = next(iter(a.state.orders.values())).snapshot
    assert order.intent.quantity == D('.45')
    h.trade(size='.2')
    assert a.state.positions['NVDAUSDT'].quantity == D('.2')
    a.close()
    h.account = RealtimeAccount(h.path, Market.BINANCE_USDM, clock=lambda: h.now,
        strategy=h.strategy, participation=D(1), slippage=D(0))
    a = h.account
    assert a.state.reconciled
    a.control('start')
    h.trade()
    assert a.state.positions['NVDAUSDT'].quantity == D('.45')
    filled = next(iter(a.state.orders.values())).snapshot
    assert filled.status == OrderStatus.FILLED
    assert filled.cumulative_fee == D('.04950')
    assert a.state.cash['USDT'] == D('999.95050')
    before = a.controller.account()
    assert not a.replay(a.state.confirmations[filled.intent.client_order_id])
    assert before == a.controller.account()
    assert not a.closed_bar(bar)
    assert len(a.state.orders) == 1
    assert a.store.db.execute("SELECT count(*) FROM events WHERE id LIKE 'signal:%'").fetchone()[0] == 21
    a.close()


def test_large_target_partial_fills_release_only_own_reservation_and_reduce(tmp_path):
    h = Runner(tmp_path / 'large', weight=D('.6'),
        limits=RiskLimits(D('1000'), D('1000'), D('1000'), 10))
    h.cross()
    h.trade(); h.trade(size='3'); h.trade()
    a = h.account
    assert a.state.positions['NVDAUSDT'].quantity == D('5.44')
    h.bar(21, '90'); h.bar(22, '90')
    h.trade(price='90'); h.trade(price='90')
    assert not a.state.positions
    assert len(a.state.orders) == 2
    assert list(a.state.orders.values())[-1].snapshot.intent.reduce_only
    a.close()


def test_reducing_target_can_close_below_minimum_notional(tmp_path):
    h = Runner(tmp_path / 'small-close')
    h.cross(); h.trade(); h.trade()
    h.bar(21, '1')
    h.trade(price='1'); h.trade(price='1')
    assert not h.account.state.positions
    assert len(h.account.state.orders) == 2
    h.account.close()


@pytest.mark.parametrize('market_step', ['0', '.001'])
def test_combined_lot_grid_applies_to_target_and_partial_fills(tmp_path, market_step):
    from copy import deepcopy
    rules = deepcopy(RULES)
    rules['symbol']['filters'][1]['stepSize'] = market_step
    h = Runner(tmp_path / market_step.replace('.', 'p'))
    h.cross()
    h.now += timedelta(milliseconds=1)
    h.send('rules', payload=rules, source='SYNTHETIC_RULES', reference='110')
    h.send('mark', price='110')
    h.send('trade', price='110', size='10')
    h.now += timedelta(milliseconds=1)
    h.send('trade', price='110', size='.253')
    assert h.account.state.positions['NVDAUSDT'].quantity == D('.25')
    assert next(iter(h.account.state.orders.values())).snapshot.intent.quantity == D('.45')
    h.account.close()


def test_insufficient_and_unclosed_bar_and_causal_crossing(tmp_path):
    h = Runner(tmp_path / 'p')
    bar = h.bar(0, '100')
    assert h.account.state.runtime['last_strategy_signal']['status'] == 'INSUFFICIENT_DATA'
    partial = replace(bar, opened_at=bar.closed_at, closed_at=bar.closed_at + timedelta(hours=1), complete=False)
    h.now = partial.closed_at
    assert not h.account.closed_bar(partial)
    assert len(h.account.state.runtime['strategy_bars']) == 1
    with pytest.raises(ValueError, match='future'):
        h.account.closed_bar(replace(partial, complete=True, closed_at=partial.closed_at + timedelta(hours=1),
            opened_at=partial.opened_at + timedelta(hours=1)))
    h.account.close()


@pytest.mark.parametrize('gate', ['kill', 'risk', 'unknown', 'disconnect'])
def test_order_gates_are_not_prediction_success(tmp_path, gate):
    limits = RiskLimits(D('1'), D('500'), D('800'), 10) if gate == 'risk' else None
    h = Runner(tmp_path / gate, limits=limits)
    h.cross()
    a = h.account
    if gate == 'kill':
        a.control('kill')
    elif gate == 'disconnect':
        a.disconnect()
    elif gate == 'unknown':
        # Missing confirmation cannot be cleared by the local restart reconciler.
        h.trade()
        order_id = next(iter(a.state.orders))
        a._change(lambda: (a.controller.broker.mark_unknown(order_id), a.state.confirmations.pop(order_id)))
    h.trade()
    if gate == 'unknown':
        assert len(a.state.orders) == 1 and not a.state.reconciled
        assert a.state.orders[order_id].snapshot.reconcile_required
    else:
        assert not a.state.orders
    assert not a.state.positions
    if gate in {'kill', 'risk'}:
        assert a.state.runtime['rejected'] == 1
        assert a.state.runtime['last_rejection'] in {'KILL_SWITCH', 'ORDER_NOTIONAL_LIMIT'}
    assert a.state.runtime['last_strategy_signal']['direction'] == 'BUY'
    a.close()


def test_strategy_version_change_refused_and_gap_pauses(tmp_path):
    h = Runner(tmp_path / 'p')
    h.bar(0, '100')
    h.bar(2, '100')
    assert h.account.state.runtime['reason'] == 'BAR_SEQUENCE_GAP'
    h.account.close()
    with pytest.raises(PersistenceConflict, match='configuration changed'):
        RealtimeAccount(h.path, Market.BINANCE_USDM, strategy=replace(h.strategy, strategy_version='2'),
                        participation=D(1), slippage=D(0))


def test_disconnect_explicit_resume_keeps_one_pending_signal(tmp_path):
    h = Runner(tmp_path / 'resume')
    h.cross()
    h.account.disconnect()
    h.trade()
    assert not h.account.state.orders
    h.account.control('start')
    h.trade(); h.trade()
    assert len(h.account.state.orders) == 1
    assert h.account.state.positions['NVDAUSDT'].quantity == D('.45')
    h.account.close()


def test_signal_and_submission_rollback_share_transaction(tmp_path, monkeypatch):
    h = Runner(tmp_path / 'p')
    h.cross()
    a = h.account
    h.now += timedelta(milliseconds=1)
    h.send('rules', payload=RULES, source='SYNTHETIC_RULES', reference='110')
    h.send('mark', price='110')
    original = a.store.record_metadata
    def fail_target(event_id, value):
        if event_id.startswith('target:'):
            raise OSError('injected journal failure')
        return original(event_id, value)
    monkeypatch.setattr(a.store, 'record_metadata', fail_target)
    with pytest.raises(OSError, match='injected'):
        h.send('trade', price='110', size='10')
    a.close()
    h.account = RealtimeAccount(h.path, Market.BINANCE_USDM, clock=lambda: h.now,
        strategy=h.strategy, participation=D(1), slippage=D(0))
    assert not h.account.state.orders
    assert h.account.state.runtime['pending_strategy_signal'] is not None
    h.account.control('start')
    h.trade(); h.trade()
    assert len(h.account.state.orders) == 1
    assert h.account.state.positions['NVDAUSDT'].quantity == D('.45')
    h.account.close()


def test_confirmed_kline_boundary_matches_replay():
    start = int(START.timestamp() * 1000)
    end = start + 3600000
    raw = dict(e='kline', s='NVDAUSDT', E=end,
        k=dict(s='NVDAUSDT', i='1h', t=start, T=end-1, x=True,
               o='100', h='110', l='99', c='105', v='123'))
    received = START + timedelta(hours=1, milliseconds=2)
    bar = closed_kline(raw, received, interval_seconds=3600)
    assert bar.closed_at == START + timedelta(hours=1)
    assert bar.open == D(100) and bar.close == D(105)
    raw['k']['x'] = False
    assert closed_kline(raw, received, interval_seconds=3600) is None
    raw['k']['x'] = True
    raw['k']['T'] -= 1
    with pytest.raises(ValueError, match='boundary'):
        closed_kline(raw, received, interval_seconds=3600)


def test_public_reader_dispatches_closed_bars_and_trades_into_shared_pipeline(tmp_path, monkeypatch):
    from factorlab.trading import realtime_service
    h = Runner(tmp_path / 'public-reader')
    for i in range(20):
        h.bar(i, '100')
    end = START + timedelta(hours=21)
    h.now = end + timedelta(milliseconds=10)
    h.send('rules', payload=RULES, source='SYNTHETIC_RULES', reference='110')
    h.send('mark', price='110')
    start_ms = int((end - timedelta(hours=1)).timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    frames = [dict(e='kline', s='NVDAUSDT', E=end_ms,
        k=dict(s='NVDAUSDT', i='1h', t=start_ms, T=end_ms-1, x=complete,
               o='110', h='110', l='110', c='110', v='100')) for complete in (False, True)]
    frames.extend(dict(e='aggTrade', s='NVDAUSDT', E=end_ms+offset, T=end_ms+offset,
                       p='110', q='10', a=sequence)
                  for sequence, offset in enumerate((11, 12), start=1))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return h.now
    monkeypatch.setattr(realtime_service, 'datetime', Clock)
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def recv(self):
            if not frames:
                raise OSError('fixture disconnect')
            value = frames.pop(0)
            h.now = end + timedelta(milliseconds=max(10, value['E']-end_ms))
            return json.dumps(value)
    def connector(url, **_):
        assert url.endswith('/nvdausdt@kline_1h')
        return Socket()
    result = asyncio.run(realtime_service.observe(h.account, network=True, seconds=1,
                                                  reconnects=0, connector=connector))
    assert result['diagnostics']['closed_bars'] == 1
    assert h.account.state.runtime['orders'] == h.account.state.runtime['fills'] == 1
    assert h.account.state.positions['NVDAUSDT'].quantity == D('.45')
    assert h.account.state.runtime['status'] == 'PAUSED'
    h.account.close()
