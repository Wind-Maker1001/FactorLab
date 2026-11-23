"""Adapters reuse the existing DualMA calculation and GridExecution lifecycle."""
from dataclasses import replace
from decimal import Decimal as D
import json

from factorlab.trading.contracts import Market, OrderStatus
from factorlab.trading.storage import dumps, decode
from factorlab.trading.realtime import identity
from factorlab.trading.strategy import ClosedBar, DualMAStrategy
from .grid import GridConfig
from .grid_execution import GridExecution, GridAccount, GridRules
from .protocol import Decision, StrategySpec


class DualMAAdapter:
    def __init__(self, symbol='NVDAUSDT', interval_seconds=3600,
                 source='BINANCE_USDM_PUBLIC_REST', max_weight='.1'):
        self.strategy = DualMAStrategy(symbol=symbol, interval_seconds=interval_seconds,
            source=source, max_weight=D(max_weight))

    def describe(self):
        s = self.strategy
        return StrategySpec(s.strategy_id, s.strategy_version, 'target', [s.symbol],
                            [s.interval_seconds], ['closed_bar'], s.history_limit)

    def decide(self, event, state):
        if event['kind'] != 'closed_bar':
            return Decision(state=state)
        signal = self.strategy.signal([ClosedBar(**b) for b in event['bars']], event['now'])
        return Decision(target_weight=signal.target_weight, state=state)


class GridAdapter:
    """Replay native Grid commands; no fill inference or economic posting.

    State contains native commands, not pickled Python objects. Existing Grid
    deployments can continue using DemoGridBridge and its compact journal.
    """
    def __init__(self, config, rules, instrument_id, initial_equity, deployment_id=None,
                 account_id=None, market='BINANCE_USDM', interval_seconds=3600):
        self.parameters = dict(config=config, rules=rules, deployment_id=deployment_id,
            account_id=account_id, market=market, instrument_id=instrument_id,
            initial_equity=initial_equity, interval_seconds=interval_seconds)

    def describe(self):
        p = self.parameters
        return StrategySpec('grid', '1', 'orders', [p['instrument_id']],
            [p['interval_seconds']], ['closed_bar', 'quote', 'order_event'])

    def _engine(self, event):
        p = self.parameters
        return GridExecution(GridConfig(**p['config']), GridRules(**{k: D(v) for k, v in p['rules'].items()}),
            deployment_id=p['deployment_id'] or event['deployment_id'],
            account_id=p['account_id'] or event['account']['account_id'], market=Market(p['market']),
            instrument_id=p['instrument_id'], initial_equity=D(p['initial_equity']))

    @staticmethod
    def _apply(grid, command):
        account = GridAccount(**command['account'])
        if command['kind'] == 'order_event':
            event = command['value']
            event = replace(event, event_id=identity(event.event_id))
            grid.confirm(event, account, delivery=grid.delivery + 1)
        else:
            grid.observe(command['value'], account, delivery=grid.delivery + 1)
            if grid.pending_bar is not None:
                grid.complete_bar(delivery=grid.delivery + 1)

    def decide(self, event, state):
        grid = self._engine(event)
        commands = decode(json.loads(state.get('commands', '[]')))
        for command in commands:
            self._apply(grid, command)
        if event['kind'] == 'order_event':
            value = event['order_event']
        else:
            quote = event.get('quote')
            bar = event.get('bar')
            at = bar['closed_at'] if bar else quote.observed_at
            value = dict(id=identity(event['event_id']), time=at.replace(tzinfo=None).isoformat(),
                price=bar['close'] if bar else quote.price, volume=0)
            if bar:
                value.update(completed_bar=True, high=bar['high'], low=bar['low'], close=bar['close'])
        command = dict(kind=event['kind'], value=value, account=event['account'])
        self._apply(grid, command)
        commands.append(command)
        return Decision(orders=tuple(o.intent for o in grid.active if o.status is OrderStatus.NEW),
            cancellations=tuple(sorted(grid.cancellations)), state={'commands': dumps(commands)})
