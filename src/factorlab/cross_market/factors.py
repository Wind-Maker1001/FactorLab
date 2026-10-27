"""Reuse the fourteen original factors with stable opaque identity keys."""
from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd

from factorlab.factors import FACTOR_REGISTRY, compute_factors
from factorlab.factors.preprocess import preprocess_cross_sections
from .contracts import ContractError


@dataclass(frozen=True)
class FactorMetadata:
    name: str
    required_fields: tuple
    supported_markets: tuple
    frequency: str
    lookback_sessions: int
    availability: str
    corporate_action_adjustment: bool
    category: str
    version: str


FACTOR_METADATA = {
    name: FactorMetadata(name, tuple('instrument_id' if x == 'symbol' else x for x in f.required_columns),
                         ('CN', 'US', 'HK'), '1d', f.lookback,
                         'all inputs known by local session close; no future adjustment vintage certified',
                         'adj_close' in f.required_columns,
                         'market_structure' if name.startswith('TURN') else 'technical_price_volume',
                         'legacy-factorlab-v1')
    for name, f in FACTOR_REGISTRY.items()
}


def metadata():
    return [asdict(v) for v in FACTOR_METADATA.values()]


def compute_instrument_batch(bars, calendar, market, names=None, holding=5, strict_vintage=True):
    """Grid only this instrument batch. Missing sessions remain missing.

    Labels enter next open and exit holding sessions later. No forward filling
    of prices, statuses, factor warmups or incomplete targets.
    """
    names = list(names or FACTOR_METADATA)
    for name in names:
        if name not in FACTOR_METADATA or market not in FACTOR_METADATA[name].supported_markets:
            raise ContractError('factor unavailable in market: ' + name)
    required = {'date', 'instrument_id', 'adj_open', 'adj_close', 'halted', 'available_at'}
    required.update(*(set(FACTOR_METADATA[n].required_fields) for n in names))
    if missing := required - set(bars):
        raise ContractError('factor data unavailable: ' + str(sorted(missing)))
    if bars.duplicated(['date', 'instrument_id']).any():
        raise ContractError('duplicate instrument/session bars')
    dates = calendar.schedule.index
    bars = bars.copy()
    if not pd.DatetimeIndex(bars.date).isin(dates).all():
        raise ContractError('bars include non-session dates')
    if strict_vintage:
        cutoffs = bars.date.map(calendar.schedule.close)
        if (pd.to_datetime(bars.available_at, utc=True) > cutoffs).any():
            raise ContractError('input unavailable at historical signal time')
    ids = sorted(bars.instrument_id.unique())
    grid = pd.MultiIndex.from_product([ids, dates], names=['instrument_id', 'date'])
    working = bars.set_index(['instrument_id', 'date']).reindex(grid).reset_index()
    # Preserve suspension observations but do not compute an execution label at
    # an untradable opening. Rolling factor history retains source prices.
    working['symbol'] = working.instrument_id
    computed = compute_factors(working, names)
    computed = computed.sort_values(['instrument_id', 'date']).reset_index(drop=True)
    tradable_open = computed.adj_open.where(computed.halted.eq(False))
    entry = tradable_open.groupby(computed.instrument_id).shift(-1)
    exit_ = tradable_open.groupby(computed.instrument_id).shift(-(holding + 1))
    computed['target'] = exit_ / entry - 1
    computed['entry_date'] = computed.date.map(pd.Series(dates, index=dates).shift(-1))
    computed['exit_date'] = computed.date.map(pd.Series(dates, index=dates).shift(-(holding + 1)))
    computed['signal_at'] = computed.date.map(calendar.schedule.close)
    # Bar absence must never create an eligible candidate on the grid.
    computed['observed'] = computed.available_at.notna()
    return computed.drop(columns='symbol')


def preprocess(frame, names=None, minimum_count=20):
    names = list(names or FACTOR_METADATA)
    return preprocess_cross_sections(frame, names, minimum_count=minimum_count)
