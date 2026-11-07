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


def _dividend_amounts(gross, rate):
    """Shared offline/in-loop gross-to-net arithmetic; unknown is never zero."""
    return gross * (1 - rate) if rate is not None else (Decimal(0) if gross == 0 else None)


class DividendExecutionLedger:
    """Incremental lifecycle used *inside* the target order loop.

    Available cash stays in the executor's single cash variable. This object
    returns only new cash deltas, and owns receivables/restricted cash. A pay
    date without intraday evidence posts AFTER_OPEN; the next observed open
    may spend it. Non-session pay dates retain their actual date in the audit.
    Gross unknown-tax assets are disclosed, never called precise net equity.
    """

    def __init__(self, plan, security_id, dates):
        from factorlab.data.corporate_actions.execution_evidence import validate_execution_plan
        self.events, self.rates = validate_execution_plan(plan, security_id, dates)
        self.eligible, self.receivables, self.paid = {}, {}, set()
        self.restricted_cash = Decimal(0)
        self.tax_accrued = Decimal(0)
        self.audit, self.entitlements, self.recognitions = [], [], []
        self.unknown_tax = False

    def _pay(self, event, observed_on, phase):
        key = event.event_id
        if key in self.paid:
            return Decimal(0)
        if key not in self.receivables:
            fail("payment before recognized entitlement")
        gross, recognized = self.receivables.pop(key)
        rate = self.rates[key]
        net = _dividend_amounts(gross, rate)
        if net is None:
            self.restricted_cash += gross
        self.audit.append(dict(event_id=key, actual_date=date(event.pay_date),
                               observed_on=observed_on, phase=phase, gross=gross,
                               net=net, available_cash_delta=net or Decimal(0),
                               restricted_cash_delta=gross if net is None else Decimal(0),
                               tax_status="UNKNOWN" if rate is None else "SYNTHETIC_EXPLICIT"))
        self.paid.add(key)
        return net or Decimal(0)

    def before_open(self, day):
        delta = Decimal(0)
        for event in self.events:
            if date(event.pay_date) < day and event.event_id not in self.paid:
                delta += self._pay(event, day, "BEFORE_OPEN_AFTER_ACTUAL_PAY_DATE")
        return float(delta)

    def after_orders(self, day, book, symbol):
        delta = Decimal(0)
        for event in self.events:
            key = event.event_id
            if day == date(event.record_date):
                if key in self.eligible:
                    fail("duplicate record-close entitlement")
                quantity = book.total_shares(symbol)
                self.eligible[key] = Decimal(quantity) * event.cash
                self.entitlements.append(dict(
                    event_id=key, revision=event.revision, record_date=day,
                    eligible_shares=quantity, cash_per_share=event.cash,
                    gross=self.eligible[key], lots=book.lot_snapshot(symbol),
                    position_source="PositionBook AFTER_OPEN_FILLS AT_RECORD_CLOSE",
                    source_id=event.source_id, source_sha256=event.source_sha256))
            if day == date(event.effective_date):
                gross = self.eligible[key]
                rate = self.rates[key]
                net = _dividend_amounts(gross, rate)
                recognized = gross if net is None else net
                self.tax_accrued += gross - recognized
                self.unknown_tax |= rate is None and gross != 0
                self.receivables[key] = (gross, recognized)
                self.recognitions.append(dict(event_id=key, effective_date=day, gross=gross,
                                              net=net, recognized=recognized))
            if day == date(event.pay_date):
                delta += self._pay(event, day, "AFTER_OPEN_ORDERS")
        return float(delta)

    def balances(self):
        return dict(receivable_gross=float(sum((r[0] for r in self.receivables.values()), Decimal(0))),
                    recognized_receivables=float(sum((r[1] for r in self.receivables.values()), Decimal(0))),
                    restricted_cash=float(self.restricted_cash), tax_accrued=float(self.tax_accrued),
                    tax_status="UNKNOWN" if self.unknown_tax else "SYNTHETIC_EXPLICIT_OR_NO_ENTITLEMENT")


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
                    net = _dividend_amounts(gross, rate)
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
