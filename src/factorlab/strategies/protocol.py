"""Decision-only strategy boundary shared by research and execution adapters.

All values cross a JSON boundary. Strategies receive observations and explicit
account projections, never a controller, gateway, store or credential provider.
"""
from dataclasses import dataclass, field, asdict
from decimal import Decimal
import json
from typing import Protocol

from factorlab.trading.contracts import OrderIntent


def json_value(value):
    return json.loads(json.dumps(value, allow_nan=False))


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    decision_kind: str
    symbols: list[str]
    intervals: list[int]
    data_types: list[str]
    history_limit: int = 1
    state_version: str = '1'

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.strategy_id, self.version, self.state_version)):
            raise ValueError('strategy identity/version required')
        if self.decision_kind not in {'target', 'orders'}:
            raise ValueError('decision_kind must be target or orders')
        if (not self.symbols or not all(isinstance(s, str) and s for s in self.symbols)
                or not self.intervals or any(type(i) is not int or i <= 0 for i in self.intervals)
                or not self.data_types or any(t not in {'closed_bar', 'quote', 'order_event'} for t in self.data_types)
                or type(self.history_limit) is not int or not 1 <= self.history_limit <= 1000):
            raise ValueError('invalid strategy data requirements')

    def require(self, *, symbol, interval_seconds, data_types):
        if (symbol not in self.symbols or interval_seconds not in self.intervals
                or not set(self.data_types) <= set(data_types)):
            raise ValueError('strategy requirements exceed execution/feed capabilities')


@dataclass(frozen=True)
class Decision:
    # None means retain the existing target. Orders retain their complete
    # identities, limit prices and cancellation/confirmation lifecycle.
    target_weight: Decimal | None = None
    orders: tuple[OrderIntent, ...] = ()
    cancellations: tuple[str, ...] = ()
    state: dict = field(default_factory=dict)

    def validate(self, spec, symbol):
        if not isinstance(self.state, dict):
            raise ValueError('strategy state must be a JSON object')
        json_value(self.state)
        if self.target_weight is not None and (not isinstance(self.target_weight, Decimal)
                or not self.target_weight.is_finite() or not 0 <= self.target_weight <= 1):
            raise ValueError('target_weight must be a Decimal within [0,1]')
        if (spec.decision_kind == 'target' and (self.orders or self.cancellations)
                or spec.decision_kind == 'orders' and self.target_weight is not None):
            raise ValueError('decision does not match declared capability')
        if (len(self.orders) > 1000 or len(self.cancellations) > 1000
                or any(not isinstance(o, OrderIntent) or o.instrument_id != symbol for o in self.orders)
                or any(not isinstance(c, str) or not c for c in self.cancellations)
                or len({o.client_order_id for o in self.orders}) != len(self.orders)):
            raise ValueError('invalid order decision')
        return self


class UnifiedStrategy(Protocol):
    def describe(self) -> StrategySpec: ...
    def decide(self, event: dict, state: dict) -> Decision:
        """Restore explicit JSON state, consume one causal event, return new state."""
        ...


def decision_payload(decision):
    return dict(target_weight=decision.target_weight, orders=list(decision.orders),
                cancellations=list(decision.cancellations), state=decision.state)


def spec_payload(spec):
    return asdict(spec)
