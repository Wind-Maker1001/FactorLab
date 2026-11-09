"""Optional prior-close liquidity proxies; not an intraday matching model."""
from dataclasses import dataclass, replace
from bisect import bisect_right
from functools import cached_property
import math

import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import ConfigurationError
from factorlab.data.schema import normalize_symbol


@dataclass(frozen=True)
class LiquidityObservation:
    """Prior-history average in shares or CNY/day, with explicit knowledge time."""
    symbol: str
    effective_date: str
    known_at: str
    average: float
    basis: str
    source: str
    source_kind: str

    def __post_init__(self):
        object.__setattr__(self, 'symbol', normalize_symbol(self.symbol))
        if self.basis not in {'shares', 'cny'}:
            raise ConfigurationError('liquidity basis must be shares or cny')
        if not math.isfinite(self.average) or self.average <= 0:
            raise ConfigurationError('historical average must be positive and finite')
        if not self.source or self.source_kind not in {'SYNTHETIC', 'HISTORICAL'}:
            raise ConfigurationError('explicit liquidity provenance required')
        effective, known = pd.Timestamp(self.effective_date), pd.Timestamp(self.known_at)
        if pd.isna(effective) or pd.isna(known) or effective.tzinfo or known.tzinfo:
            raise ConfigurationError('liquidity timestamps must be valid local times')
        if effective.normalize() > known.normalize():
            raise ConfigurationError('liquidity cannot be known before its effective date')


@dataclass(frozen=True)
class ResearchExecutionScenario:
    """Additional explicit fees and prior-history participation limits.

    impact_bps = impact_coefficient_bps * sqrt(order / historical_average).
    Order and average use the same shares or CNY units. Fixed and impact costs
    are subcomponents of the existing slippage charge, never price shifts.
    """
    fixed_slippage_bps: float = 0.0
    participation_limit: float | None = None
    impact_coefficient_bps: float = 0.0
    fill_policy: str = 'partial'
    liquidity: tuple[LiquidityObservation, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'liquidity', tuple(self.liquidity))
        for value in (self.fixed_slippage_bps, self.impact_coefficient_bps):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ConfigurationError('research cost rates must be finite and nonnegative')
        if self.participation_limit is not None and (
            isinstance(self.participation_limit, bool) or not math.isfinite(self.participation_limit)
            or not 0 < self.participation_limit <= 1
        ):
            raise ConfigurationError('participation limit must be in (0, 1]')
        if self.fill_policy not in {'partial', 'reject'}:
            raise ConfigurationError('fill_policy must be partial or reject')
        identities = [(x.symbol, pd.Timestamp(x.effective_date), pd.Timestamp(x.known_at)) for x in self.liquidity]
        if len(set(identities)) != len(identities):
            raise ConfigurationError('duplicate liquidity observation')

    @cached_property
    def _history_index(self):
        histories = {}
        for observation in self.liquidity:
            histories.setdefault(observation.symbol, []).append(observation)
        index = {}
        for symbol, observations in histories.items():
            times, winners, winner = [], [], None
            for observation in sorted(observations, key=lambda x: pd.Timestamp(x.known_at)):
                if winner is None or (pd.Timestamp(observation.effective_date), pd.Timestamp(observation.known_at)) > (
                    pd.Timestamp(winner.effective_date), pd.Timestamp(winner.known_at)
                ):
                    winner = observation
                times.append(pd.Timestamp(observation.known_at))
                winners.append(winner)
            index[symbol] = times, winners
        return index

    def observation(self, symbol, signal_date):
        cutoff = pd.Timestamp(signal_date).normalize() + pd.Timedelta(hours=15)
        times, winners = self._history_index.get(symbol, ((), ()))
        offset = bisect_right(times, cutoff)
        return winners[offset - 1] if offset else None

    @property
    def needs_liquidity(self):
        return self.participation_limit is not None or self.impact_coefficient_bps > 0


class ResearchFeeQuote:
    """Order-local quotation consumed by the existing cash affordability search."""
    def __init__(self, schedule: FeeSchedule, scenario, observation, price):
        self.schedule, self.scenario, self.observation, self.price = schedule, scenario, observation, price

    def components(self, notional):
        fixed = notional * self.scenario.fixed_slippage_bps / 10_000
        impact = 0.0
        if self.scenario.impact_coefficient_bps:
            demand = notional / self.price if self.observation.basis == 'shares' else notional
            impact = notional * self.scenario.impact_coefficient_bps * math.sqrt(demand / self.observation.average) / 10_000
        return fixed, impact

    def calculate(self, trade_date, side, notional):
        base = self.schedule.calculate(trade_date, side, notional)
        fixed, impact = self.components(notional)
        return replace(base, slippage=base.slippage + fixed + impact)
