"""Small capability contracts; suppliers retain their own native schemas."""
from dataclasses import dataclass, field
from typing import Protocol, Iterable

import pandas as pd


class ContractError(ValueError):
    pass


class ValuationError(ContractError):
    """Fail-closed valuation error with the last completed accounting state.

    Partial results end BEFORE the failed session. They are never a completed
    backtest, and missing-date values must not be replaced by last-known marks.
    """
    def __init__(self, message, *, date, issues, cash, quantities, partial):
        super().__init__(message)
        self.date, self.issues = date, issues
        self.cash, self.quantities, self.partial = cash, dict(quantities), partial


def session_date(value):
    value = pd.Timestamp(value)
    if pd.isna(value) or value.tz is not None or value != value.normalize():
        raise ContractError('session label must be a naive midnight date, not an instant')
    return value


def instant(value):
    value = pd.Timestamp(value)
    if pd.isna(value) or value.tz is None:
        raise ContractError('an aware historical available/execution timestamp is required')
    return value.tz_convert('UTC')


class MarketCalendar(Protocol):
    market: str
    timezone: str
    def sessions(self, start, end) -> pd.DataFrame: ...
    def shift(self, date, count: int): ...


class UniverseProvider(Protocol):
    def members(self, date, as_of) -> pd.DataFrame: ...


class DataProvider(Protocol):
    version: str
    def batches(self, start, end, instrument_ids=None, columns=None, batch_size=65536) -> Iterable[pd.DataFrame]: ...


class FeeModel(Protocol):
    def charge(self, date, side: str, quantity: float, price: float) -> float: ...


class CorporateActionProvider(Protocol):
    def events(self, start, end, as_of) -> pd.DataFrame: ...
    def require_coverage(self, start, end, instrument_ids): ...


@dataclass(frozen=True)
class ResearchSpec:
    market: str
    universe: str
    start_date: str
    end_date: str
    benchmark: str
    currency: str
    frequency: str = '1d'
    model: tuple = ('FactorScore', 'Ridge', 'LightGBM', 'XGBoost')
    rebalance_frequency: int = 5
    top_k: tuple = (20, 50)
    train_months: int = 12
    validation_months: int = 3
    test_months: int = 3
    purge_sessions: int = 5
    embargo_sessions: int = 5
    holding_sessions: int = 5
    factor_names: tuple = ()
    research_only: bool = True

    def __post_init__(self):
        if self.market not in {'CN', 'US'}:
            raise ContractError('market adapter not implemented')
        if self.currency != {'CN': 'CNY', 'US': 'USD'}[self.market]:
            raise ContractError('cross-currency portfolio requires verified FX; unsupported')
        allowed = {'CN': {'CSI500', 'ALL_CN_A_SHARE'}, 'US': {'ALL_US_COMMON', 'US_INDEX'}}
        if self.universe not in allowed[self.market] or not self.benchmark:
            raise ContractError('explicit compatible universe and benchmark required')
        if session_date(self.start_date) > session_date(self.end_date):
            raise ContractError('start_date exceeds end_date')
        if self.frequency != '1d' or not self.research_only:
            raise ContractError('daily isolated research only; no formal/forward consumer')
        if not self.model or not set(self.model) <= {'FactorScore', 'Ridge', 'LightGBM', 'XGBoost'}:
            raise ContractError('unknown/empty model selection')
        for name in ('rebalance_frequency', 'train_months', 'validation_months', 'test_months', 'holding_sessions'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ContractError(name + ' must be a positive integer')
        if not self.top_k or any(type(k) is not int or k < 1 for k in self.top_k):
            raise ContractError('positive Top-K required')
        if any(type(v) is not int or v < 0 for v in (self.purge_sessions, self.embargo_sessions)):
            raise ContractError('purge/embargo must be nonnegative integers')
