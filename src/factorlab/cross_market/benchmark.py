"""Explicit local-currency price/total-return benchmark contract."""
import numpy as np
import pandas as pd
from .contracts import ContractError, instant


def validate_benchmark(frame, calendar, start, end, currency):
    required = {'date', 'close', 'available_at', 'currency', 'return_definition', 'benchmark_id'}
    if required - set(frame) or frame.date.duplicated().any():
        raise ContractError('benchmark missing fields or duplicate sessions')
    if set(frame.currency) != {currency} or frame.return_definition.nunique() != 1 or frame.benchmark_id.nunique() != 1:
        raise ContractError('benchmark currency/definition/identity inconsistent')
    if not frame.return_definition.isin(['price_return', 'total_return']).all():
        raise ContractError('benchmark return definition must be explicit')
    dates = calendar.sessions(start, end).index
    f = frame.set_index('date').reindex(dates)
    if not np.isfinite(f.close).all() or (f.close <= 0).any() or f.available_at.isna().any():
        raise ContractError('benchmark coverage/price missing')
    for value in f.available_at:
        instant(value)
    return f.reset_index().rename(columns={'index': 'date'})


def attach_benchmark(nav, benchmark):
    f = nav.merge(benchmark[['date', 'close', 'return_definition']], on='date', how='left', validate='one_to_one')
    if f.close.isna().any():
        raise ContractError('benchmark missing valuation dates')
    f['benchmark_nav'] = f.close / f.close.iloc[0]
    f['relative_nav_ratio'] = f.nav / f.benchmark_nav
    return f.rename(columns={'return_definition': 'benchmark_return_definition'})
