from decimal import Decimal as D
import json
from urllib.parse import parse_qs

import pytest

from test_binance_demo_execution import DemoFixture, setup
from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_symbols import DemoSymbolRegistry
from factorlab.integrations.binance_futures_trading.fake import NOW, synthetic_rules
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading import Market, OrderIntent, OrderType, Side
from factorlab.trading.control import RiskRejected
from factorlab.trading.paper import Instrument, Quote


class SymbolsFixture(DemoFixture):
    def __init__(self):
        super().__init__()
        self.status = 'TRADING'
        self.symbols = {'NVDAUSDT': (1, '.01', '.01'),
                        'BTCUSDT': (5, '.1', '.001'),
                        'ETHUSDT': (3, '.001', '.1')}

    def __call__(self, request, timeout):
        if request.path == '/fapi/v1/exchangeInfo':
            rows = []
            for symbol, (_, tick, step) in self.symbols.items():
                rules = synthetic_rules()
                filters = {k: dict(v) for k, v in rules.filters.items()}
                filters['PRICE_FILTER']['tickSize'] = tick
                filters['LOT_SIZE']['stepSize'] = step
                rows.append(dict(symbol=symbol, status=self.status, quoteAsset='USDT',
                    marginAsset='USDT', contractType=('TRADIFI_PERPETUAL' if symbol == 'NVDAUSDT'
                                                   else 'PERPETUAL'), timeInForce=['GTC'],
                    orderTypes=['LIMIT'], filters=list(filters.values())))
            return Response(200, {}, json.dumps(dict(symbols=rows)).encode())
        if request.path == '/fapi/v1/leverageBracket':
            symbol = parse_qs(request.query)['symbol'][0]
            return Response(200, {}, json.dumps([dict(symbol=symbol,
                brackets=list(synthetic_rules().brackets))]).encode())
        if request.path == '/fapi/v1/symbolConfig':
            return Response(200, {}, json.dumps([dict(symbol=s, leverage=v[0],
                marginType='CROSSED') for s, v in self.symbols.items()]).encode())
        return super().__call__(request, timeout)


def test_dynamic_rules_and_leverage_do_not_switch_shared_gateway(monkeypatch):
    adapter, gateway, controller, fake = setup(fake=SymbolsFixture())
    registry = DemoSymbolRegistry(adapter, controller.store)
    try:
        original = gateway.rules
        original_observation = adapter._rule_observation
        for symbol in ('BTCUSDT', 'ETHUSDT', 'NVDAUSDT'):
            if symbol == 'NVDAUSDT':
                assert adapter._rule_observation is original_observation
            context = registry.observe(symbol)
            gateway.register_symbol(context)
            controller._atomic(lambda: gateway.state.instruments.update({symbol: Instrument(symbol, 'USDT')}))
            controller.quote(Quote(symbol, D(100), NOW))
        assert gateway.rules is original
        assert gateway.execution_rules('BTCUSDT')[1:] == (5, 5)
        assert gateway.execution_rules('ETHUSDT')[1:] == (3, 3)
        assert gateway.execution_rules('NVDAUSDT')[1:] == (1, 1)
        class ReachedDispatch(Exception):
            pass
        monkeypatch.setattr(adapter, 'submit_order', lambda intent: (_ for _ in ()).throw(ReachedDispatch()))
        for symbol, quantity, price, approved in (
                ('BTCUSDT', '.1', '100.1', True),
                ('ETHUSDT', '.1', '100.001', True),
                ('BTCUSDT', '.1', '100.001', False),
                ('ETHUSDT', '.11', '100.001', False),
                ('NVDAUSDT', '.1', '100.01', True)):
            intent = OrderIntent(Market.BINANCE_USDM, symbol, Side.BUY,
                OrderType.LIMIT, D(quantity), D(price), symbol + price)
            with pytest.raises(ReachedDispatch if approved else RiskRejected):
                gateway.submit_order(intent)
        assert not any(method != 'GET' for method, *_ in fake.calls)
    finally:
        controller.store.close()
        adapter.store.close()


def test_unverified_unknown_and_halted_symbols_never_register():
    adapter, gateway, controller, fake = setup(fake=SymbolsFixture())
    registry = DemoSymbolRegistry(adapter, controller.store)
    try:
        with pytest.raises(SafetyBlocked, match='observed Demo rules'):
            gateway.execution_rules('BTCUSDT')
        fake.status = 'HALT'
        with pytest.raises(ValueError, match='currently trading'):
            registry.observe('BTCUSDT')
        assert 'BTCUSDT' not in registry.contexts
        assert not any(method != 'GET' for method, *_ in fake.calls)
    finally:
        controller.store.close()
        adapter.store.close()
