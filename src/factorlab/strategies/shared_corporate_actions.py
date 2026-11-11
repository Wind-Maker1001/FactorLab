"""Evidence-gated corporate-action overlay on immutable shared-account receipts."""
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from factorlab.backtest.corporate_actions import _dividend_amounts
from factorlab.errors import SchemaError
from factorlab.strategies.real_shared_portfolio import audit_accounting


@dataclass(frozen=True)
class CashDividendEvidence:
    event_id: str
    symbol: str
    source_id: str
    event_type: str = 'CASH_DIVIDEND'
    announcement_date: str | None = None
    public_at: str | None = None
    record_date: str | None = None
    ex_date: str | None = None
    cash_per_share: str | None = None
    implementation_evidence_id: str | None = None
    payment_date: str | None = None
    payment_evidence_id: str | None = None
    tax_rate: str | None = None
    tax_evidence_id: str | None = None
    public_time_precision: str = 'SECOND'
    issuer_payment_evidence_id: str | None = None
    settlement_rule_evidence_id: str | None = None


def saved_path_accounting(nav, trades, positions, events, *, initial_cash, price_basis='RAW',
                          settlement_basis='ACTUAL_ACCOUNT'):
    """Post verified rights and settlements, without reinvesting or certifying coverage.

    Record-session close holdings determine eligibility; ex-date recognizes gross
    receivables. Actual account payments require account evidence; the explicit
    simulated basis instead requires issuer payment and clearing-rule evidence.
    Both transfer receivables once; unknown withholding is not spendable net cash.
    Missing dates/implementation evidence remain unresolved, not zero dividends.
    """
    if settlement_basis not in ('ACTUAL_ACCOUNT', 'SIMULATED_BY_RULE'):
        raise SchemaError('unknown settlement basis')
    if price_basis != 'RAW':
        raise SchemaError('adjusted prices would double-count cash dividends')
    audit_accounting(SimpleNamespace(nav=nav, trades=trades, positions=positions),
                     positions[['symbol']], initial_cash)
    days = pd.DatetimeIndex(nav.date)
    if days.has_duplicates or not days.is_monotonic_increasing or days.empty:
        raise SchemaError('unique ordered account sessions required')
    events = tuple(events)
    if len({e.event_id for e in events}) != len(events):
        raise SchemaError('resolve duplicate/revised economic events before posting')
    economic_keys = [(e.symbol, e.record_date, e.ex_date, e.event_type) for e in events
                     if e.record_date and e.ex_date]
    if len(set(economic_keys)) != len(economic_keys):
        raise SchemaError('resolve duplicate/revised economic events before posting')
    holdings = positions.set_index(['date', 'symbol']).shares
    postings, rights, unresolved = [], [], []
    for event in events:
        reason = None
        if not event.source_id:
            raise SchemaError('event source identity required')
        if event.event_type != 'CASH_DIVIDEND':
            reason = 'UNSUPPORTED_SHARE_CHANGE'
        elif not all((event.record_date, event.ex_date, event.cash_per_share,
                      event.implementation_evidence_id, event.public_at)):
            reason = 'IMPLEMENTATION_OR_TERMS_UNVERIFIED'
        if reason:
            unresolved.append(dict(event_id=event.event_id, symbol=event.symbol,
                                   state='EVENT_UNRESOLVED', reason=reason))
            continue
        record, ex = pd.Timestamp(event.record_date), pd.Timestamp(event.ex_date)
        public = pd.Timestamp(event.public_at)
        # Public timestamps must use the exchange's explicit local wall-clock convention.
        if public.tz is not None:
            public = public.tz_convert('Asia/Shanghai').tz_localize(None)
        if event.public_time_precision not in ('DAY', 'SECOND'):
            raise SchemaError('explicit public time precision required')
        # A date-only notice cannot establish availability before that day's close.
        if (not record < ex or public.normalize() > record
                or (event.public_time_precision == 'DAY' and public.normalize() == record)):
            raise SchemaError('invalid implementation chronology')
        if record not in days or ex not in days:
            unresolved.append(dict(event_id=event.event_id, symbol=event.symbol,
                                   state='EVENT_UNRESOLVED', reason='LIFECYCLE_OUTSIDE_SAVED_SESSIONS'))
            continue
        cash = Decimal(event.cash_per_share)
        if not cash.is_finite() or cash < 0:
            raise SchemaError('finite nonnegative per-share cash required')
        shares = int(holdings.get((record, event.symbol), 0))
        gross = cash * shares
        rate = None
        if event.tax_rate is not None:
            if not event.tax_evidence_id:
                raise SchemaError('tax rate requires account-specific evidence')
            rate = Decimal(event.tax_rate)
            if not rate.is_finite() or not 0 <= rate <= 1:
                raise SchemaError('invalid evidenced withholding rate')
        net = _dividend_amounts(gross, rate)
        rights.append(dict(event_id=event.event_id, symbol=event.symbol, record_date=record,
                          ex_date=ex, eligible_shares=shares, gross_entitlement=float(gross),
                          tax_status='TAX_UNVERIFIED' if net is None else 'EVIDENCED_OR_NO_ENTITLEMENT',
                          source_id=event.source_id))
        for day, state in ((public.normalize(), 'DIVIDEND_DECLARED'), (record, 'DIVIDEND_ENTITLED')):
            postings.append(dict(event_id=event.event_id, date=day, state=state,
                                 receivable_delta=0., gross_cash_delta=0., tax_delta=0.))
        postings.append(dict(event_id=event.event_id, date=ex, state='DIVIDEND_RECEIVABLE',
                             receivable_delta=float(gross), gross_cash_delta=0., tax_delta=0.))
        if net is None:
            postings.append(dict(event_id=event.event_id, date=ex, state='TAX_UNVERIFIED',
                                 receivable_delta=0., gross_cash_delta=0., tax_delta=None))
        actual_payment = bool(event.payment_date and event.payment_evidence_id)
        simulated_payment = bool(event.payment_date and event.issuer_payment_evidence_id
                                 and event.settlement_rule_evidence_id)
        if settlement_basis == 'SIMULATED_BY_RULE':
            payment_verified = simulated_payment
            payment_state = 'SIMULATED_SETTLEMENT_BY_RULE'
        else:
            payment_verified = actual_payment
            payment_state = 'DIVIDEND_CASH_PAID'
        if not actual_payment:
            unresolved.append(dict(event_id=event.event_id, symbol=event.symbol,
                                   state='EVENT_UNRESOLVED', reason='ACTUAL_PAYMENT_UNVERIFIED'))
        if not payment_verified:
            continue
        pay = pd.Timestamp(event.payment_date)
        if pay < ex:
            raise SchemaError('actual payment precedes receivable')
        # Keep the actual date on the journal, including non-session settlement.
        postings.append(dict(event_id=event.event_id, date=pay, state=payment_state,
                             receivable_delta=-float(gross), gross_cash_delta=float(gross),
                             tax_delta=None if net is None else float(gross - net)))
    journal = pd.DataFrame(postings, columns=['event_id', 'date', 'state', 'receivable_delta',
                                              'gross_cash_delta', 'tax_delta'])
    table = nav[['date', 'cash', 'portfolio_value', 'transaction_cost']].copy()
    signed = trades.trade_notional.where(trades.side.eq('sell'), -trades.trade_notional) - trades.transaction_cost
    flow = signed.groupby(trades.execution_date).sum()
    table['trade_cash_flow'] = table.date.map(flow).fillna(0.)
    table['market_value'] = table.portfolio_value - table.cash
    table['raw_price_pnl_net_trading_costs'] = table.portfolio_value - initial_cash
    table['raw_price_pnl_before_explicit_trading_costs'] = (
        table.raw_price_pnl_net_trading_costs + table.transaction_cost.cumsum())
    for column, delta in (('receivable_gross', 'receivable_delta'), ('paid_cash_gross', 'gross_cash_delta')):
        table[column] = [float(journal.loc[journal.date.le(day), delta].sum()) for day in table.date]
    table['settlement_basis'] = settlement_basis
    table['actual_account_payment_verified'] = settlement_basis == 'ACTUAL_ACCOUNT' and all(
        e.payment_date and e.payment_evidence_id for e in events) and bool(events) and not unresolved
    table['simulated_cash_gross'] = table.paid_cash_gross if settlement_basis == 'SIMULATED_BY_RULE' else 0.
    table['cash_gross_sensitivity'] = table.cash + table.paid_cash_gross
    table['bounded_equity_gross'] = table.cash_gross_sensitivity + table.market_value + table.receivable_gross
    recognized = journal.loc[journal.state.eq('DIVIDEND_RECEIVABLE')]
    income = [float(recognized.loc[recognized.date.le(day), 'receivable_delta'].sum()) for day in table.date]
    table['dividend_income_gross'] = income
    table['dividend_residual'] = table.dividend_income_gross - table.receivable_gross - table.paid_cash_gross
    table['equity_residual'] = table.bounded_equity_gross - table.portfolio_value - table.dividend_income_gross
    table['cash_residual'] = table.cash_gross_sensitivity - initial_cash - table.trade_cash_flow.cumsum() - table.paid_cash_gross
    # Coverage and unsupported actions have not been certified by this overlay.
    table['investor_total_return'] = None
    table['dividend_tax'] = [None if unresolved or journal.loc[journal.date.le(day), 'tax_delta'].isna().any()
                             else float(journal.loc[journal.date.le(day), 'tax_delta'].sum())
                             for day in table.date]
    table['settlement_cash_net'] = table.cash_gross_sensitivity - pd.to_numeric(table.dividend_tax)
    return dict(schema='P9_C02_SAVED_PATH_V1', ledger=table, journal=journal,
                entitlements=pd.DataFrame(rights), unresolved=pd.DataFrame(unresolved),
                total_return=None, total_return_status='TOTAL_RETURN_UNAVAILABLE',
                scope='BOUNDED_GROSS_CORPORATE_ACTION_SENSITIVITY_NOT_REEXECUTION')
