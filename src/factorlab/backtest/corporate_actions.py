"""Reusable, explicit pre-tax dividend ledger; no financial admission.

Accepts close holdings and net trade cash flows from any execution adapter.
Entitlement is fixed at record close, receivable appears ex-date, cash on pay
date. Raw close marks are mandatory. Unknown tax never becomes a net receipt.
Unsupported share changes are refused before any account state is computed.
"""
from decimal import Decimal, localcontext

import pandas as pd

from factorlab.data.corporate_actions.validation import decimal, event_terms
from factorlab.data.evidence import date, fail


def cash_dividend_ledger(positions, events, coverage, *, security_id, initial_cash, tax_rates=None,
                         price_basis="RAW"):
    if price_basis != "RAW":
        fail("raw tradable marks required; adjusted prices would double-count dividends")
    required = {"date", "shares", "raw_close", "trade_cash_flow"}
    if not isinstance(positions, pd.DataFrame) or positions.empty or not required.issubset(positions):
        fail("explicit daily holdings, raw marks and net trade cash flows required")
    dates = pd.DatetimeIndex(positions.date)
    if (dates.tz is not None or dates.hasnans or dates.has_duplicates or not dates.is_monotonic_increasing
            or not dates.equals(dates.normalize())):
        fail("unique ordered daily position observations required")
    cover = [c for c in coverage if c.security_id == security_id and c.status == "COMPLETE"
             and date(c.start) <= dates[0] and date(c.end) >= dates[-1]]
    if not cover:
        fail("INCOMPLETE corporate action coverage; absence is not proof of no events")
    events = tuple(events)
    ids, terms = set(), {}
    for event in events:
        if event.security_id != security_id or event.event_id in ids:
            fail("wrong security or duplicate/revised event; resolve revisions explicitly")
        ids.add(event.event_id)
        if event.event_type != "CASH_DIVIDEND":
            fail("UNSUPPORTED share-change execution; explicit future contract required")
        terms[event.event_id] = event_terms(event)
        record, effective, pay = map(date, (event.record_date, event.effective_date, event.pay_date))
        if not dates[0] <= record < effective <= pay <= dates[-1]:
            fail("event lifecycle outside complete position observations or invalid payment ordering")
        if any(day not in dates for day in (record, effective, pay)):
            fail("record/ex/payment date missing from position observations")
    cash = decimal(initial_cash, "initial cash")
    tax_rates = {} if tax_rates is None else tax_rates
    if set(tax_rates) - ids:
        fail("unknown tax event identity")
    tax = {key: decimal(value, "tax rate") for key, value in tax_rates.items()}
    if any(v > 1 for v in tax.values()):
        fail("tax rate exceeds one")
    eligible, receivable, paid, rows, audit = {}, {}, set(), [], []
    net_cash, net_known = cash, True
    with localcontext() as context:
        context.prec = 50
        for row in positions.itertuples(index=False):
            shares = decimal(str(row.shares), "shares")
            if shares != shares.to_integral_value():
                fail("integer observed share quantity required")
            mark = decimal(str(row.raw_close), "raw close", positive=True)
            try:
                flow = Decimal(str(row.trade_cash_flow))
            except Exception:
                fail("finite trade cash flow required")
            if not flow.is_finite():
                fail("finite trade cash flow required")
            cash += flow
            net_cash += flow
            gross_paid = Decimal(0)
            net_paid = Decimal(0)
            for event in events:
                key = event.event_id
                if row.date == date(event.record_date):
                    eligible[key] = shares
                if row.date == date(event.effective_date):
                    receivable[key] = eligible[key] * terms[key]["cash"]
                if row.date == date(event.pay_date):
                    if key in paid:
                        fail("duplicate dividend payment")
                    gross = receivable.pop(key)
                    cash += gross
                    gross_paid += gross
                    rate = tax.get(key)
                    net = gross * (1 - rate) if rate is not None else (Decimal(0) if gross == 0 else None)
                    if net is None:
                        net_known = False
                    else:
                        net_cash += net
                        net_paid += net
                    audit.append(dict(event_id=key, security_id=security_id, record_date=event.record_date,
                                      effective_date=event.effective_date, pay_date=event.pay_date,
                                      eligible_shares=eligible[key], gross=gross, net=net,
                                      tax_status="KNOWN_EXPLICIT_ASSUMPTION" if rate is not None else "UNKNOWN",
                                      source_sha256=event.source_sha256))
                    paid.add(key)
            outstanding = sum(receivable.values(), Decimal(0))
            value = shares * mark
            if cash < 0:
                fail("negative cash in dividend ledger")
            rows.append(dict(date=row.date, shares=shares, raw_close=mark, cash_gross=cash,
                             cash_net=net_cash if net_known else None, receivable_gross=outstanding,
                             dividend_gross=gross_paid, dividend_net=net_paid if net_known else None,
                             market_value=value, equity_gross=cash + value + outstanding,
                             tax_status="KNOWN_EXPLICIT_ASSUMPTION" if net_known else "UNKNOWN"))
    return dict(ledger=pd.DataFrame(rows), payments=pd.DataFrame(audit),
                scope="PRETAX_SOFTWARE_ONLY", net_status="KNOWN_EXPLICIT_ASSUMPTION" if net_known else "BLOCKED_TAX_UNKNOWN",
                financial_admission="FORMAL_ADMISSION_BLOCKED")
