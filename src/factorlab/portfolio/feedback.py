"""Optimize actual close holdings, then queue targets for the next open."""
from dataclasses import dataclass, replace

import pandas as pd

from factorlab.data.schema import normalize_symbol
from factorlab.errors import SchemaError
from .optimizer import OptimizationInput, OptimizationResult, PortfolioOptimizer, _labels, _vector


def _local_time(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is not None:
        raise SchemaError("feedback times must be naive A-share exchange-local timestamps")
    return stamp


@dataclass(frozen=True)
class SaleRestriction:
    """Explicit evidence that shares cannot be sold through a future date.

    A close suspension observation alone is not evidence of next-day suspension.
    """
    symbol: str
    shares: int
    known_at: object
    through: object
    reason: str


@dataclass(frozen=True)
class FeedbackDecision:
    snapshot: object
    signal_time: pd.Timestamp
    decision_time: pd.Timestamp
    execution_time: pd.Timestamp
    optimization: OptimizationResult
    corporate_actions_status: str = "UNKNOWN"


class ExecutionFeedback:
    """No retry, constraint relaxation, target substitution or history replay."""

    def __init__(self, session, optimizer: PortfolioOptimizer, *, require_verified_actions=False):
        self.session = session
        self.optimizer = optimizer
        self.require_verified_actions = require_verified_actions

    def optimize(self, data: OptimizationInput, *, signal_time=None, sale_restrictions=()):
        snapshot = self.session.snapshot
        decision = _local_time(data.decision_time)
        if decision != snapshot.as_of:
            raise SchemaError("decision_time must equal the current known close snapshot")
        if data.covariance_as_of is not None:
            _local_time(data.covariance_as_of)
        if data.returns is not None and (not isinstance(data.returns.index, pd.DatetimeIndex)
                                         or data.returns.index.tz is not None):
            raise SchemaError("feedback returns require naive exchange-local timestamps")
        signal = snapshot.as_of if signal_time is None else _local_time(signal_time)
        if signal > decision:
            raise SchemaError("signal is not yet observable at decision_time")
        if self.require_verified_actions:
            # TODO: integrate evidence-qualified corporate-action plans into this session.
            raise SchemaError("corporate action completeness UNKNOWN: financial use blocked")
        if data.previous_weights is not None:
            raise SchemaError("feedback owns previous_weights; use actual session holdings")
        if any(normalize_symbol(s) != s for s in data.symbols):
            raise SchemaError("feedback requires canonical execution symbols")
        execution_day = self.session.next_execution_date
        floors = {}
        if data.minimum_weights is not None:
            if not _labels(data.minimum_weights.index):
                raise SchemaError("minimum_weights require unique string symbols")
            names = sorted(data.minimum_weights.index)
            values = _vector(data.minimum_weights, names, "minimum_weights")
            if (values < 0).any():
                raise SchemaError("minimum_weights must be nonnegative")
            floors = dict(zip(names, values))
        holdings = {h.symbol: h for h in snapshot.holdings}
        for h in snapshot.holdings:
            locked = h.shares - self.session.sellable_next_open(h.symbol)
            floors[h.symbol] = max(floors.get(h.symbol, 0.), locked*h.mark_price/snapshot.nav)
        restricted = {}
        for restriction in sale_restrictions:
            h = holdings.get(restriction.symbol)
            if (h is None or isinstance(restriction.shares, bool)
                    or not isinstance(restriction.shares, int)
                    or not 0 <= restriction.shares <= h.shares or not restriction.reason):
                raise SchemaError("sale restriction requires held shares and an explicit reason")
            if _local_time(restriction.known_at) > decision:
                raise SchemaError("future sale restriction evidence is forbidden")
            through = pd.Timestamp(restriction.through)
            if pd.isna(through) or through.tzinfo is not None or through != through.normalize():
                raise SchemaError("restriction through must be a local daily date")
            if through >= execution_day:
                restricted[h.symbol] = max(restricted.get(h.symbol, 0), restriction.shares)
        for symbol, shares in restricted.items():
            h = holdings[symbol]
            floors[symbol] = max(floors.get(symbol, 0.), shares*h.mark_price/snapshot.nav)
        result = self.optimizer.optimize(replace(data, previous_weights=snapshot.security_weights,
            minimum_weights=pd.Series(floors, dtype=float) if floors else None))
        self.session.submit_targets(result.target_weights, optimizer_turnover=result.turnover,
                                    locked_shares=restricted)
        return FeedbackDecision(snapshot, signal, snapshot.as_of,
            execution_day + pd.Timedelta(hours=9, minutes=30), result)

    def advance(self):
        return self.session.advance()
