"""Bounded, non-executable conditional marks for independent research accounts."""
from dataclasses import dataclass, asdict
import hashlib
import json
import math

import pandas as pd

from factorlab.errors import SchemaError
from .session import ExecutionSession, _day

LABELS = ('SCENARIO_ONLY', 'NOT_HISTORICAL_VALUATION_CERTIFIED', 'NOT_EXECUTION_PRICE',
          'RIGHTS_UNSETTLED', 'NO_LIVE_MONEY')


@dataclass(frozen=True)
class ScenarioValuation:
    research_id: str
    symbol: str
    start: str
    end_exclusive: str
    mark: float
    source: str
    hindsight: bool = False
    public_after: str | None = None
    before_public_mark: float | None = None
    version: str = 'v1'
    source_document_sha256: str | None = None

    def __post_init__(self):
        if _day(self.start) >= _day(self.end_exclusive):
            raise SchemaError('SCENARIO_EMPTY_WINDOW')
        for price in (self.mark, self.before_public_mark):
            if price is not None and (not math.isfinite(price) or price < 0):
                raise SchemaError('SCENARIO_INVALID_MARK')
        if self.public_after is not None:
            _day(self.public_after)
            if self.before_public_mark is None:
                raise SchemaError('SCENARIO_PUBLICATION_REQUIRES_PRIOR_ASSUMPTION')

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def active(self, day):
        return _day(self.start) <= day < _day(self.end_exclusive)

    def close_mark(self, day, symbol, row):
        if symbol != self.symbol or not self.active(day):
            return None
        if row is None or row.suspended != 1 or pd.notna(row.raw_close):
            raise SchemaError(f'SCENARIO_REASSESS_SOURCE_STATE: {day.date()}/{symbol}')
        if self.public_after is not None and day <= _day(self.public_after):
            return self.before_public_mark
        return self.mark


class ScenarioExecutionSession(ExecutionSession):
    """Explicit research account; no change to ordinary session or Sidecar contracts."""

    def __init__(self, engine, bars, benchmark, *, signal_date, valuation):
        self.scenario_valuation = valuation
        super().__init__(engine, bars, benchmark, signal_date=signal_date)

    def advance(self):
        if self.next_execution_date >= _day(self.scenario_valuation.end_exclusive):
            raise SchemaError('SCENARIO_EVENT_REASSESSMENT_REQUIRED: ' + self.scenario_valuation.end_exclusive)
        return super().advance()
