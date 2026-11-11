"""Field-scoped evidence consumption, separate from observed execution prices."""
from dataclasses import asdict

import numpy as np
import pandas as pd

from factorlab.data.market_evidence_sidecar import POLICY
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_study import execution_bars as original_execution_bars
from factorlab.strategies.real_shared_portfolio import audit_accounting


OBSERVED_ONLY = 'OBSERVED_ONLY'
KEYS = ['date', 'symbol']
VALUATION_COLUMNS = [*KEYS, 'shares', 'raw_close', 'valuation_price', 'book_value',
    'valuation_status', 'valuation_policy', 'valuation_policy_version',
    'valuation_source_date', 'valuation_source_price_sha256',
    'suspension_document_sha256', 'nontradable']


def execution_bars(bars, evidence, *, cutoff):
    """Consume only the exact ST fact; target-day execution remains unknown.

    Raw vendor status and OHLC survive unchanged. All non-target rows retain the
    original execution adapter's mapping, including its handling of unknown enums.
    """
    data = original_execution_bars(bars)
    target = data.symbol.eq('000540.SZ') & pd.to_datetime(data.date).eq(pd.Timestamp('2023-05-08'))
    if target.any():
        fact = evidence.fact('000540.SZ', '2023-05-08', 'is_st',
                             purpose='SIGNAL_STATE', cutoff=cutoff)
        data.loc[target, 'is_st'] = int(fact.candidate_value)
        data.loc[target, 'trade_status'] = np.nan
    return data


def value_positions(positions, bars, evidence, *, policy, previous_observations):
    """Return one valuation per existing holding, never patching a raw bar.

    ``previous_observations`` maps symbols to the verified observation capsules
    accepted by MarketEvidence.carry. Unsupported missing marks remain unvaluable;
    invalid or incomplete evidence for an in-scope carry fails at consumption.
    """
    if policy not in (OBSERVED_ONLY, POLICY):
        raise ValueError('unknown valuation policy')
    held = positions.loc[positions.shares.gt(0), KEYS + ['shares']]
    marked = held.merge(bars[KEYS + ['raw_close']], on=KEYS, how='left', validate='one_to_one')
    scoped = {(f.security, f.day) for f in evidence.facts if f.field == 'valuation_mark'}
    rows = []
    for position in marked.itertuples(index=False):
        day = pd.Timestamp(position.date).strftime('%Y-%m-%d')
        raw = position.raw_close
        row = dict.fromkeys(VALUATION_COLUMNS)
        row.update(date=position.date, symbol=position.symbol, shares=position.shares,
                   raw_close=raw, valuation_policy=policy, nontradable=True,
                   valuation_status='UNVALUABLE_POSITION')
        if pd.notna(raw) and np.isfinite(float(raw)) and float(raw) > 0:
            row.update(valuation_price=raw, book_value=position.shares * raw,
                       valuation_status='OBSERVED_MARKET_PRICE', valuation_source_date=day,
                       nontradable=None)
        elif policy == POLICY and pd.isna(raw) and (position.symbol, day) in scoped:
            if position.shares != int(position.shares):
                raise SchemaError('nonintegral existing holding quantity')
            carry = evidence.carry(position.symbol, day, shares=int(position.shares),
                raw_close=None, previous_observation=previous_observations.get(position.symbol))
            fields = asdict(carry)
            row.update({key: value for key, value in fields.items() if key in row and key != 'raw_close'})
            row['book_value'] = carry.book_value
        rows.append(row)
    return pd.DataFrame(rows, columns=VALUATION_COLUMNS)


def audit_valuation_ledger(result, bars, initial_cash, evidence, *, policy, previous_observations):
    """Reconcile receipts and positions against explicit policy-valued holdings.

    Accounting uses the shared receipt audit; valuation eligibility is checked
    separately without manufacturing raw closes. An unvaluable holding blocks
    equity reconciliation.
    """
    marks = value_positions(result.positions, bars, evidence, policy=policy,
                           previous_observations=previous_observations)
    if marks.valuation_status.eq('UNVALUABLE_POSITION').any():
        raise SchemaError('UNVALUABLE_POSITION')
    nav, trades, positions = result.nav, result.trades, result.positions

    def equal(left, right, message, *, atol=1e-7):
        if not np.allclose(np.asarray(left, dtype=float), np.asarray(right, dtype=float),
                           rtol=1e-12, atol=atol):
            raise SchemaError(message)

    audit_accounting(result, bars, initial_cash)
    reconciled = positions.merge(marks[KEYS + ['valuation_price', 'book_value']], on=KEYS,
                                 how='left', validate='one_to_one')
    equal(reconciled.mark_price, reconciled.valuation_price, 'position mark differs from valuation policy', atol=0)
    equal(reconciled.market_value, reconciled.book_value, 'position valuation does not reconcile')
    value = marks.assign(book_value=marks.book_value.astype(float)).groupby('date').book_value.sum().reindex(nav.date, fill_value=0).to_numpy()
    equal(nav.cash.to_numpy() + value, nav.portfolio_value, 'equity ledger does not reconcile')
    return dict(status='PASSED', cash=True, shares=True, equity=True, fees=True,
                valuation_policy=policy, policy_marks=True,
                observed_positions=int(marks.valuation_status.eq('OBSERVED_MARKET_PRICE').sum()),
                assumed_positions=int(marks.valuation_status.eq('MODEL_VALUATION_ASSUMPTION').sum()))
