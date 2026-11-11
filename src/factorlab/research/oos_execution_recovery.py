"""P9-02 field-scoped recovery; source bars remain immutable."""
import numpy as np
import pandas as pd

from factorlab.data.market_evidence_sidecar import POLICY
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.oos_execution_study import execution_bars


CONDITIONAL = 'CONDITIONAL_ON_CONFIRMED_SUSPENSION_CARRY'
TRADE_STATUS = 'RETROSPECTIVE_VENDOR_DAILY_STATE_NOT_PREOPEN_KNOWLEDGE_OR_FILL_PERMISSION'


def recovery_bars(bars, evidence, *, cutoff):
    data = execution_bars(bars)
    target = data.symbol.eq('000540.SZ') & data.date.eq(pd.Timestamp('2023-05-08'))
    fact = evidence.fact('000540.SZ', '2023-05-08', 'is_st',
        purpose='SIGNAL_STATE', cutoff=cutoff)
    if (target.sum() != 1 or not data.loc[target, 'st_status'].eq(2).all()
            or not data.loc[target, 'suspended'].eq(0).all()
            or fact.candidate_value is not True):
        raise SchemaError('TARGETED_ST_SOURCE_DRIFT: 2023-05-08/000540.SZ')
    data.loc[target, 'is_st'] = int(fact.candidate_value)
    return data


def audit_frozen_fees(result, config):
    """Recalculate each receipt through the same frozen fee schedule."""
    from datetime import date
    from factorlab.backtest.fees import FeeRule, FeeSchedule
    schedule = FeeSchedule([FeeRule(effective_from=date(1990, 1, 1), **config['fees'])])
    for trade in result.trades.itertuples(index=False):
        expected = schedule.calculate(trade.execution_date, trade.side, trade.trade_notional)
        for field in ('commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage'):
            if not np.isclose(getattr(trade, field), getattr(expected, field), rtol=1e-12, atol=1e-7):
                raise SchemaError(f'FROZEN_FEE_RECEIPT_MISMATCH: {trade.execution_date}/{trade.symbol}/{field}')


def qualify_valuations(result, bars, evidence, observation):
    """Check engine marks against observed closes or independently approved carry."""
    rows = result.positions.merge(bars[['date', 'symbol', 'raw_close', 'suspended']],
        on=['date', 'symbol'], how='left', validate='one_to_one')
    output = []
    nav = result.nav.set_index('date').portfolio_value
    for row in rows.itertuples(index=False):
        day = str(row.date.date())
        observed = row.raw_close
        assumed = not np.isfinite(observed) or observed <= 0
        if assumed:
            if not pd.isna(observed) or row.suspended != 1:
                raise SchemaError(f'UNQUALIFIED_HELD_CLOSE: {day}/{row.symbol}')
            try:
                value = evidence.carry(row.symbol, day, shares=int(row.shares),
                    raw_close=None, previous_observation=observation)
            except DataQualityError as exc:
                raise SchemaError(f'UNQUALIFIED_HELD_CLOSE: {day}/{row.symbol}: {exc}') from exc
            price = float(value.valuation_price)
            source_date, policy, status = value.valuation_source_date, POLICY, value.valuation_status
            identity = value.suspension_document_sha256 + ':' + value.valuation_source_price_sha256
            fills = result.trades.loc[result.trades.execution_date.eq(row.date) & result.trades.symbol.eq(row.symbol)]
            if len(fills):
                raise SchemaError(f'TRADE_ON_CARRY_SESSION: {day}/{row.symbol}')
            prior = result.positions.loc[result.positions.symbol.eq(row.symbol) & result.positions.date.lt(row.date)]
            if prior.empty or prior.iloc[-1].shares != row.shares:
                raise SchemaError(f'CARRY_REQUIRES_EXISTING_UNCHANGED_HOLDING: {day}/{row.symbol}')
        else:
            price, source_date = float(observed), day
            policy, status, identity = 'OBSERVED_RAW_CLOSE', 'OBSERVED_RAW_CLOSE', evidence.source_manifest_sha256
        if row.mark_price != price:
            raise SchemaError(f'ENGINE_MARK_DIFFERS_FROM_QUALIFIED_PRICE: {day}/{row.symbol}')
        output.append(dict(date=row.date, symbol=row.symbol, shares=int(row.shares),
            observed_raw_close=None if pd.isna(observed) else float(observed),
            valuation_price=price, valuation_source_date=source_date, valuation_policy=policy,
            valuation_status=status, evidence_identity=identity,
            affected_portfolio_weight=row.shares * price / nav.loc[row.date],
            valuation_assumption_used=assumed, confirmed_suspended=row.suspended == 1))
    return pd.DataFrame(output, columns=['date', 'symbol', 'shares', 'observed_raw_close',
        'valuation_price', 'valuation_source_date', 'valuation_policy', 'valuation_status',
        'evidence_identity', 'affected_portfolio_weight', 'valuation_assumption_used', 'confirmed_suspended'])
