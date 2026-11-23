"""Trusted target strategy; identical decision code in research and Demo."""
from decimal import Decimal
from factorlab.strategies.protocol import Decision, StrategySpec


class CloseMomentum:
    def __init__(self, symbol='NVDAUSDT', interval_seconds=60, weight='.01'):
        self.symbol, self.interval, self.weight = symbol, interval_seconds, Decimal(weight)

    def describe(self):
        return StrategySpec('close_momentum', '1.0.0', 'target', [self.symbol],
            [self.interval], ['closed_bar'], history_limit=2)

    def decide(self, event, state):
        bars = event['bars']
        state = {'decisions': state.get('decisions', 0) + 1}
        if len(bars) < 2:
            return Decision(state=state)
        weight = self.weight if bars[-1]['close'] > bars[-2]['close'] else Decimal(0)
        return Decision(target_weight=weight, state=state)
