"""Manually recomputable entitlement, receivable, payment and raw-mark cases."""
from dataclasses import replace
from decimal import Decimal as D

import pandas as pd
import pytest

from factorlab.backtest.corporate_actions import cash_dividend_ledger
from factorlab.errors import DataQualityError
from tests.research.market_data_fixture import event, evidence, snapshot


def inputs(tmp_path):
    artifact = evidence(tmp_path)
    action = event(artifact, pay_date="2024-01-08")
    positions = pd.DataFrame(dict(date=pd.bdate_range("2024-01-02", periods=5),
                                  shares=[100] * 5, raw_close=[100, 100, 98, 98, 98],
                                  trade_cash_flow=[-10000, 0, 0, 0, 0]))
    return positions, action, snapshot(artifact).coverage


def run(positions, actions, coverage, **kwargs):
    return cash_dividend_ledger(positions, actions, coverage, security_id="fixture-security",
                                initial_cash=10000, **kwargs)


def test_record_ex_and_payment_are_separate_and_nav_reconciles(tmp_path):
    p, a, c = inputs(tmp_path)
    result = run(p, [a], c, tax_rates={a.event_id: "0.1"})
    ledger = result["ledger"]
    assert ledger.cash_gross.tolist() == [D(0), D(0), D(0), D(0), D(200)]
    assert ledger.receivable_gross.tolist() == [D(0), D(0), D(200), D(200), D(0)]
    assert ledger.equity_gross.tolist() == [D(10000)] * 5
    assert ledger.cash_net.iloc[-1] == D(180)
    assert result["payments"].iloc[0].eligible_shares == 100
    assert result["payments"].iloc[0].gross == 200
    assert result["financial_admission"] == "FORMAL_ADMISSION_BLOCKED"


def test_sale_after_record_does_not_lose_entitlement(tmp_path):
    p, a, c = inputs(tmp_path)
    p.loc[2:, "shares"] = 0
    p.loc[2, "trade_cash_flow"] = 9800
    result = run(p, [a], c)
    assert result["ledger"].equity_gross.tolist() == [D(10000)] * 5
    assert result["ledger"].cash_gross.iloc[-1] == 10000


def test_empty_record_or_ex_date_buy_never_receives_dividend(tmp_path):
    p, a, c = inputs(tmp_path)
    p["shares"] = [0, 0, 100, 100, 100]
    p["trade_cash_flow"] = [0, 0, -9800, 0, 0]
    result = run(p, [a], c)
    assert result["payments"].iloc[0].gross == 0
    assert result["ledger"].cash_gross.iloc[-1] == 200


def test_unknown_tax_is_not_reported_as_net_receipt(tmp_path):
    p, a, c = inputs(tmp_path)
    result = run(p, [a], c)
    assert result["net_status"] == "BLOCKED_TAX_UNKNOWN"
    assert result["payments"].iloc[0].net is None
    assert result["ledger"].cash_net.iloc[-1] is None


@pytest.mark.parametrize("field,value,match", [("record_date", None, "record date"),
    ("pay_date", None, "payment date"), ("pay_date", "2024-01-03", "ordering"),
    ("pay_date", "2024-01-06", "missing"), ("event_type", "BONUS", "UNSUPPORTED"),
    ("status", "PROPOSED", "implemented")])
def test_incomplete_or_unsupported_events_fail_before_posting(tmp_path, field, value, match):
    p, a, c = inputs(tmp_path)
    with pytest.raises(DataQualityError, match=match):
        run(p, [replace(a, **{field: value})], c)


def test_missing_coverage_duplicate_payment_and_adjusted_marks_refused(tmp_path):
    p, a, c = inputs(tmp_path)
    for actions, coverage, kwargs, match in [([a], (), {}, "coverage"),
         ([a, a], c, {}, "duplicate"), ([a], c, {"price_basis": "ADJUSTED"}, "double-count")]:
        with pytest.raises(DataQualityError, match=match):
            run(p, actions, coverage, **kwargs)
