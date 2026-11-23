"""Custom order strategy demonstrating exact limit intents and confirmations."""
from decimal import Decimal as D
from factorlab.strategies.protocol import StrategySpec, Decision
from factorlab.trading.contracts import Market, OrderIntent, OrderStatus, OrderType, Side


class LimitOnce:
    def __init__(self, symbol='NVDAUSDT', interval_seconds=60, quantity='.04', offset='5'):
        self.symbol, self.interval = symbol, interval_seconds
        self.quantity, self.offset = D(quantity), D(offset)

    def describe(self):
        return StrategySpec('limit_once', '1.0.0', 'orders', [self.symbol],
            [self.interval], ['quote', 'order_event'])

    def decide(self, event, state):
        if event['kind'] == 'quote' and not state.get('issued'):
            intent = OrderIntent(event['account']['market'], self.symbol, Side.BUY,
                OrderType.LIMIT, self.quantity, event['quote'].price - self.offset,
                event['deployment_id'] + '-entry', position_side='BOTH')
            return Decision(orders=(intent,), state={'issued': True})
        return Decision(state=state)
