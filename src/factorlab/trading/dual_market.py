"""Deterministic synthetic dual-market execution; no network transport or credentials."""
from dataclasses import asdict
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from factorlab.integrations.binance_equity.market import stream_rows
from factorlab.integrations.binance_equity_trading.paper import Observations, StockAdapter, StockPaperBroker
from factorlab.integrations.binance_equity_trading.protocol import Rules
from factorlab.integrations.binance_futures_trading.adapter import FuturesAdapter
from factorlab.integrations.binance_futures_trading.fake import FakeTransport, NOW, MS, synthetic_rules
from factorlab.integrations.binance_futures_trading.gateway import FuturesPaperGateway
from factorlab.integrations.binance_futures_trading.protocol import Credentials, FuturesClient
from factorlab.integrations.binance_futures_trading.stream import UserDataEvents
from . import Market, OrderIntent, OrderType, SAFETY_LABELS, Side
from .control import RiskLimits, TradingController
from .paper import Instrument, Quote
from .storage import PaperStore


class SyntheticAccounts:
    """One explicit store per account plus the existing futures protocol journal."""

    def __init__(self, root):
        self.root = Path(root)
        self.transport = FakeTransport()
        self.protocol_store = PaperStore(self.root / 'futures-protocol.sqlite')
        client = FuturesClient(Credentials('synthetic-key', 'synthetic-secret'),
            transport=self.transport, clock=lambda: NOW.timestamp())
        self.futures_adapter = FuturesAdapter(client, self.protocol_store, clock=lambda: NOW)
        self.futures_adapter.reconcile()
        self.futures_store = PaperStore(self.root / 'futures-paper.sqlite')
        self.stocks_store = PaperStore(self.root / 'stocks-paper.sqlite')
        self.futures = self.futures_controller()
        self.stocks = self.stocks_controller()
        self.futures.quote(Quote('NVDAUSDT', D('100'), NOW))
        self.stocks.quote(Quote('NVDA', D('150'), NOW))
        _, quotes = stream_rows(dict(e='quote', s='NVDA', E=MS, T=MS,
            bp='149.99', ap='150', bs='10', **{'as': '10'}), NOW.isoformat(), 'synthetic:quote')
        _, sessions = stream_rows(dict(e='calendar', ts=MS, **{'from': 'PRE', 'to': 'O'}),
            NOW.isoformat(), 'synthetic:session')
        rules = Rules.from_exchange_info(dict(symbol='NVDA', tradability='BUY_SELL',
            stepSize='0.001', minQty='0.001', maxQty='1000', minNotional='1', maxNotional='100000',
            fractionable=True, fractionableEh=True, extendedSession=True, overnightSupported=True,
            maxNumOrders=100, multiplierDown='0.5', multiplierUp='1.5'))
        self.observations = Observations.from_equity(quotes[0], sessions[0], rules,
            rules_time=MS, rules_received_at=NOW, permission=True, reference_price=D('150'))

    def futures_controller(self):
        gateway = FuturesPaperGateway(self.futures_adapter, synthetic_rules(),
            (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('1000')})
        return TradingController(gateway, self.futures_store, clock=lambda: NOW,
            limits=RiskLimits(D('500'), D('500'), D('800'), 10))

    def stocks_controller(self):
        broker = StockPaperBroker('synthetic-stocks-usd', (Instrument('NVDA', 'USD'),),
            {'USD': D('1000')}, fee_rate=D('0.001'))
        return TradingController(broker, self.stocks_store, clock=lambda: NOW,
            limits=RiskLimits(D('500'), D('500'), D('800'), 10))

    def signals(self):
        if self.observations.price > D('151'):
            raise ValueError('no synthetic buy signal')
        return (OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
                    D('2'), D('100'), 'futures-nvda-signal-1'),
                OrderIntent(Market.BINANCE_EQUITY, 'NVDA', Side.BUY, OrderType.MARKET,
                    D('0.5'), None, 'stocks-nvda-signal-00000000000001'))

    def close(self):
        for store in (self.protocol_store, self.futures_store, self.stocks_store):
            store.close()


def run(root):
    accounts = SyntheticAccounts(root)
    try:
        f, s = accounts.futures, accounts.stocks
        fi, si = accounts.signals()
        params = s.broker.bind(si, accounts.observations, notional=D('75'))
        f.submit(fi)
        s.submit(si)
        stream = UserDataEvents(accounts.futures_adapter)
        first = stream.parse(accounts.transport.trade_update(fi.client_order_id, '0.75'))
        f.replay(first)
        partial = s.fill(si.client_order_id, D('0.2'))
        assert not s.replay(partial)
        assert not f.replay(first)
        authority = s.account()
        second = stream.parse(accounts.transport.trade_update(fi.client_order_id, '1.25'))
        stream.disconnected()
        accounts.futures = f = accounts.futures_controller()
        assert not f.account().reconciled
        assert f.broker.recover(f).reconciled
        recovered = f.account()
        f.replay(second)
        assert f.account() == recovered
        assert not f.replay(second)
        assert s.account() == authority
        s.cancel(si.client_order_id)
        sell = OrderIntent(Market.BINANCE_EQUITY, 'NVDA', Side.SELL, OrderType.MARKET,
            D('0.1'), None, 'stocks-nvda-sell-0000000000000001')
        s.broker.bind(sell, accounts.observations)
        s.submit(sell)
        assert s.broker.synthetic_holdings()[0]['sellable'] == D('0.1')
        s.fill(sell.client_order_id, D('0.05'))
        s.cancel(sell.client_order_id)
        expected = s.account()
        futures_before = f.account()
        accounts.stocks = s = accounts.stocks_controller()
        assert StockAdapter(s).reconcile(expected).reconciled
        assert f.account() == futures_before
        fa, sa = f.reconcile(), s.account()
        assert fa.reconciled and sa.reconciled
        assert (fa.assets[0].total, fa.assets[0].reserved, fa.positions[0].quantity) == (D('999.8'), D('200'), D('2'))
        assert (sa.assets[0].total, sa.assets[0].reserved, sa.positions[0].quantity) == (D('977.4625'), D('0'), D('0.15'))
        return dict(labels=SAFETY_LABELS, inputs='SYNTHETIC_QUOTES_AND_RULES',
            futures=asdict(fa), stocks=asdict(sa), stock_request=params,
            fees={'USDT': '0.2', 'USD': '0.0375'}, settlement='UNKNOWN',
            recovery='QUERY_AND_RECONCILE_ONLY', duplicate_booking=False)
    finally:
        accounts.close()


def main():
    with TemporaryDirectory(prefix='factorlab-dual-market-') as root:
        print(json.dumps(run(root), default=str, indent=2))


if __name__ == '__main__':
    main()
