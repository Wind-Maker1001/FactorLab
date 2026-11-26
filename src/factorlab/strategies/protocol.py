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
    historical_observations: bool = False

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.strategy_id, self.version, self.state_version)):
            raise ValueError('strategy identity/version required')
        if self.decision_kind not in {'target', 'orders'}:
            raise ValueError('decision_kind must be target or orders')
        if (not self.symbols or not all(isinstance(s, str) and s for s in self.symbols)
                or not self.intervals or any(type(i) is not int or i <= 0 for i in self.intervals)
                or not self.data_types or any(t not in {'closed_bar', 'quote', 'order_event'} for t in self.data_types)
                or type(self.history_limit) is not int or not 1 <= self.history_limit <= 1000
                or type(self.historical_observations) is not bool
                or self.historical_observations and (self.decision_kind != 'orders'
                    or 'closed_bar' not in self.data_types)):
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


V1 = 'FACTORLAB_DECISION_V1'
V2 = 'FACTORLAB_DECISION_V2'
DECISION_KINDS = frozenset({'target_weight', 'target_weights', 'target_quantities', 'quantity_deltas', 'orders'})
BINDING_FIELDS = frozenset({'market', 'account_id', 'instance_id', 'allocation_id', 'strategy_id'})
OBSERVATION_TYPES = frozenset({'closed_bar', 'quote', 'order_event', 'funding_forecast',
                               'funding_settlement', 'volatility'})
REFERENCE_TYPES = (OBSERVATION_TYPES - {'order_event'}) | {'feature', 'prediction'}


def _validate_inputs(data, symbols):
    references = data.get('references', {})
    if not isinstance(references, dict) or set(references) & set(symbols):
        raise ValueError('reference symbols must be separate from trading symbols')
    if references and data['data_types'][0] != 'closed_bar':
        raise ValueError('reference data requires a closed-bar decision event')
    total_history = 0
    for symbol, streams in references.items():
        if (not isinstance(symbol, str) or not symbol.strip() or not isinstance(streams, dict)
                or not streams or not set(streams) <= REFERENCE_TYPES):
            raise ValueError('invalid reference streams')
        for kind, requirement in streams.items():
            fields = {'source', 'history_limit'}
            if kind == 'closed_bar':
                fields |= {'price_basis', 'interval_seconds'}
            if (not isinstance(requirement, dict) or set(requirement) != fields
                    or not isinstance(requirement['source'], str)
                    or not requirement['source'].strip() or type(requirement['history_limit']) is not int
                    or not 1 <= requirement['history_limit'] <= 1000):
                raise ValueError('reference streams require an explicit source and bounded history')
            if kind == 'closed_bar' and (not isinstance(requirement['price_basis'], str)
                    or not requirement['price_basis'].strip() or type(requirement['interval_seconds']) is not int
                    or requirement['interval_seconds'] <= 0):
                raise ValueError('reference bars require an explicit price basis and interval')
            total_history += requirement['history_limit']
    if total_history > 4096:
        raise ValueError('reference histories exceed the event budget')
    for name in ('account_fields', 'allocation_fields'):
        fields = data.get(name, [])
        if (not isinstance(fields, list) or any(not isinstance(field, str) or not field.strip() for field in fields)
                or len(fields) != len(set(fields))):
            raise ValueError('invalid projection field declaration: ' + name)


def validate_binding(binding):
    from factorlab.trading.contracts import Market
    if (set(binding) != BINDING_FIELDS
            or any(not isinstance(value, str) or not value.strip() for value in binding.values())):
        raise ValueError('complete instance binding required')
    Market(binding['market'])
    return binding


@dataclass(frozen=True)
class StrategySpecV2:
    strategy_id: str
    version: str
    market: str
    account_id: str
    symbols: list[str]
    decision_kind: str
    data: dict
    schedule: dict
    state: dict
    capital: dict
    execution: dict

    def __post_init__(self):
        from factorlab.trading.contracts import Market, decimal_value
        Market(self.market)
        if any(not isinstance(v, str) or not v.strip() for v in (self.strategy_id, self.version, self.account_id)):
            raise ValueError('strategy identity/version/account required')
        if (not self.symbols or len(self.symbols) > 1000
                or any(not isinstance(s, str) or not s for s in self.symbols)
                or len(set(self.symbols)) != len(self.symbols) or self.decision_kind not in DECISION_KINDS):
            raise ValueError('invalid strategy symbols/decision kind')
        data_types = self.data['data_types']
        if (not data_types or len(set(data_types)) != len(data_types)
                or not set(data_types) <= OBSERVATION_TYPES
                or type(self.data['history_limit']) is not int or not 1 <= self.data['history_limit'] <= 1000
                or type(self.data['historical_observations']) is not bool):
            raise ValueError('invalid strategy data requirements')
        _validate_inputs(self.data, self.symbols)
        if self.schedule['kind'] == 'interval':
            if (set(self.schedule) != {'kind', 'interval_seconds'}
                    or type(self.schedule['interval_seconds']) is not int or self.schedule['interval_seconds'] <= 0):
                raise ValueError('invalid interval schedule')
        elif self.schedule['kind'] == 'calendar':
            if (set(self.schedule) != {'kind', 'calendar_id', 'timezone', 'event'}
                    or any(not isinstance(v, str) or not v for v in self.schedule.values())):
                raise ValueError('invalid calendar schedule')
        else:
            raise ValueError('unknown schedule kind')
        if (not isinstance(self.state['version'], str) or not self.state['version']
                or self.state['migration'] not in {'reset', 'preserve', 'migrate'}
                or not isinstance(self.state['from_versions'], list)
                or any(not isinstance(v, str) or not v for v in self.state['from_versions'])
                or self.state['migration'] == 'migrate' and not self.state['from_versions']):
            raise ValueError('invalid state migration requirements')
        if not isinstance(self.capital['currency'], str) or not self.capital['currency']:
            raise ValueError('capital currency required')
        decimal_value(self.capital['requested'], 'requested capital')
        if (not set(self.execution['order_types']) <= {'LIMIT', 'MARKET'}
                or any(not isinstance(v, str) or not v for v in self.execution['time_in_force'])
                or any(type(self.execution[k]) is not bool for k in ('shorting', 'fractional_quantities', 'reduce_only'))):
            raise ValueError('invalid execution requirements')

    @property
    def state_version(self):
        return self.state['version']

    def require_binding(self, binding):
        validate_binding(binding)
        if any(binding[k] != getattr(self, k) for k in ('market', 'account_id', 'strategy_id')):
            raise ValueError('strategy binding mismatch')


@dataclass(frozen=True)
class DecisionV2:
    kind: str
    targets: dict[str, Decimal] = field(default_factory=dict)
    orders: tuple[OrderIntent, ...] = ()
    cancellations: tuple[dict, ...] = ()
    state: dict = field(default_factory=dict)

    def validate(self, spec, symbol=None):
        if not isinstance(spec, StrategySpecV2) or self.kind != spec.decision_kind:
            raise ValueError('decision does not match V2 declaration')
        if not isinstance(self.state, dict):
            raise ValueError('strategy state must be a JSON object')
        json_value(self.state)
        if (not isinstance(self.targets, dict) or not set(self.targets) <= set(spec.symbols)
                or len(self.targets) > 1000 or self.kind == 'target_weight' and len(self.targets) > 1):
            raise ValueError('invalid target symbols')
        for value in self.targets.values():
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError('targets must be finite Decimals')
            if value < 0 and not spec.execution['shorting'] and self.kind != 'quantity_deltas':
                raise ValueError('negative position requires shorting capability')
            if self.kind == 'target_weight' and not 0 <= value <= 1:
                raise ValueError('single target weight outside [0,1]')
            if (self.kind in {'target_quantities', 'quantity_deltas'}
                    and not spec.execution['fractional_quantities'] and value != value.to_integral_value()):
                raise ValueError('fractional quantity not declared')
        if (self.kind == 'orders' and self.targets
                or self.kind != 'orders' and (self.orders or self.cancellations)):
            raise ValueError('mixed decision modes')
        if (len(self.orders) > 1000 or len(self.cancellations) > 1000
                or any(not isinstance(o, OrderIntent) or o.instrument_id not in spec.symbols
                       or o.market.value != spec.market or o.order_type.value not in spec.execution['order_types']
                       or o.time_in_force not in spec.execution['time_in_force']
                       or o.reduce_only and not spec.execution['reduce_only']
                       or not spec.execution['fractional_quantities'] and o.quantity != o.quantity.to_integral_value()
                       for o in self.orders)
                or len({o.client_order_id for o in self.orders}) != len(self.orders)):
            raise ValueError('invalid V2 order decision')
        for cancel in self.cancellations:
            if (not isinstance(cancel, dict) or set(cancel) != {'symbol', 'client_order_id'}
                    or cancel['symbol'] not in spec.symbols
                    or not isinstance(cancel['client_order_id'], str) or not cancel['client_order_id']):
                raise ValueError('invalid V2 cancellation')
        if len({(c['symbol'], c['client_order_id']) for c in self.cancellations}) != len(self.cancellations):
            raise ValueError('duplicate cancellation')
        return self


class UnifiedStrategyV2(Protocol):
    def describe(self) -> StrategySpecV2: ...
    def decide(self, event: dict, state: dict) -> DecisionV2: ...
    def migrate_state(self, state: dict, from_version: str) -> dict: ...


def decision_payload(decision):
    if isinstance(decision, DecisionV2):
        return dict(protocol=V2, kind=decision.kind, targets=decision.targets,
                    orders=list(decision.orders), cancellations=list(decision.cancellations), state=decision.state)
    return dict(target_weight=decision.target_weight, orders=list(decision.orders),
                cancellations=list(decision.cancellations), state=decision.state)


def spec_payload(spec):
    payload = asdict(spec)
    if isinstance(spec, StrategySpecV2):
        payload['protocol'] = V2
    return payload


def read_spec(payload):
    payload = dict(payload)
    protocol = payload.pop('protocol', V1)
    if protocol not in {V1, V2}:
        raise ValueError('unsupported strategy protocol')
    return (StrategySpecV2 if protocol == V2 else StrategySpec)(**payload)


def read_decision(payload):
    payload = dict(payload)
    protocol = payload.pop('protocol', V1)
    if protocol not in {V1, V2}:
        raise ValueError('unsupported decision protocol')
    return (DecisionV2 if protocol == V2 else Decision)(**payload)
