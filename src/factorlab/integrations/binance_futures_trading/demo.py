"""Synthetic NVDAUSDT signal → risk → adapter → fake → update → reconcile."""
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from factorlab.trading import Market, OrderIntent, OrderType, SAFETY_LABELS, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore

from .adapter import FuturesAdapter
from .fake import FakeTransport, NOW, synthetic_rules
from .gateway import FuturesPaperGateway
from .protocol import Credentials, FuturesClient
from .stream import UserDataEvents


def run(root):
    transport = FakeTransport()
    client = FuturesClient(Credentials('synthetic-key', 'synthetic-secret'), transport=transport, clock=lambda: NOW.timestamp())
    adapter_store, paper_store = PaperStore(Path(root) / 'protocol.sqlite'), PaperStore(Path(root) / 'paper.sqlite')
    adapter = FuturesAdapter(client, adapter_store, clock=lambda: NOW)
    adapter.reconcile()
    gateway = FuturesPaperGateway(adapter, synthetic_rules(), (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D('1000')})
    controller = TradingController(gateway, paper_store,
        limits=RiskLimits(D('500'), D('500'), D('800'), 10), clock=lambda: NOW)
    controller.quote(Quote('NVDAUSDT', D('100'), NOW))
    signal = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT, D('2'), D('100'), 'nvda-signal-1')
    controller.submit(signal)
    events = UserDataEvents(adapter)
    event = events.parse(transport.trade_update(signal.client_order_id, '0.75'))
    controller.replay(event)
    duplicate = events.parse(transport.trade_update(signal.client_order_id, '1.25'))
    controller.replay(duplicate)
    assert not controller.replay(duplicate)
    account = controller.reconcile()
    assert account.reconciled
    assert account.positions[0].quantity == D('2')
    assert account.positions[0].entry_price == D('100')
    assert account.assets[0].total == D('999.8')
    assert account.assets[0].reserved == D('200')
    assert not account.open_orders
    assert adapter.order(signal.client_order_id).cumulative_fee == D('0.2')
    result = {'symbol': 'NVDAUSDT', 'status': 'FILLED', 'quantity': '2', 'entry_price': '100',
              'wallet_USDT': str(account.assets[0].total), 'collateral_USDT': str(account.assets[0].reserved),
              'fees_USDT': '0.2', 'reconciled': account.reconciled}
    adapter_store.close()
    paper_store.close()
    return result


def main():
    print(' / '.join(SAFETY_LABELS))
    with TemporaryDirectory(prefix='factorlab-futures-') as root:
        print(run(root))


if __name__ == '__main__':
    main()
