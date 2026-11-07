"""Deterministic integration oracles: no provider data or financial admission."""
from dataclasses import replace
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.corporate_actions import cash_dividend_ledger
from factorlab.backtest.execution import ExecutionBacktestConfig, PositionBook
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.backtest.target import run_target_positions
from factorlab.data.corporate_actions.contracts import EventCoverage, SourceSnapshot
from factorlab.data.corporate_actions.execution_evidence import (
    EXECUTION_FIELDS, DividendExecutionPlan, DividendTax, EventSupplement,
)
from factorlab.data.evidence import json_bytes, sha256, snapshot_file
from factorlab.errors import DataQualityError, SchemaError
from factorlab.strategies.dual_ma import (
    DualMAConfig, run_software_backtest, run_integrated_software_backtest,
)
from tests.research.market_data_fixture import event


KNOWN = "2023-12-02T00:00:00+08:00"
ACQUIRED = "2024-12-31T00:00:00+08:00"
SYMBOL = "600000.SH"


def artifact(root, name, payload):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(json_bytes(payload))
    return snapshot_file(path, "SYNTHETIC_EXECUTION_EVIDENCE", ACQUIRED)


def plan_for(tmp_path, dates, *, pay="2024-01-08", rate="0", events=None):
    source = artifact(tmp_path, "source.json", {"scope": "SYNTHETIC_ONLY"})
    if events is None:
        events = [event(source, security_id=SYMBOL, revision=2, available_at=KNOWN, pay_date=pay)]
    supplements, artifacts = [], [source]
    for i, action in enumerate(events):
        original = replace(action, revision=action.revision-1, status="PROPOSED", record_date=None,
                           available_at="2023-12-01", source_sha256=source.sha256)
        action = replace(action, source_sha256=source.sha256)
        events[i] = action
        link = artifact(tmp_path, "link-%d.json" % i, dict(
            original_sha256=sha256(json_bytes(original)), implemented_sha256=sha256(json_bytes(action)),
            source_version="synthetic-v1", fields={field: dict(value=str(getattr(action, field)),
                source_sha256=source.sha256, locator="synthetic terms/"+field, available_at=KNOWN)
                for field in EXECUTION_FIELDS}))
        artifacts.append(link)
        supplements.append(EventSupplement(original, "synthetic-v1", link))
    start, end = str(dates[0].date()), str(dates[-1].date())
    coverage_source = artifact(tmp_path, "coverage.json", dict(
        security_id=SYMBOL, start=start, end=end,
        event_revisions=sorted([[e.event_id, e.revision] for e in events]),
        completeness_statement="EXHAUSTIVE SYNTHETIC FIXTURE; no external market claim"))
    artifacts.append(coverage_source)
    coverage = EventCoverage(SYMBOL, start, end, KNOWN, coverage_source.sha256, "COMPLETE")
    snap = SourceSnapshot("SYNTHETIC_EXECUTION_EVIDENCE", "synthetic-v1", ACQUIRED,
                          tuple(events), (coverage,), tuple(artifacts), (), True)
    taxes = tuple(DividendTax(e.event_id, "SYNTHETIC_EXPLICIT", rate, "SYNTHETIC_FLAT_RATE_V1")
                  for e in events) if rate is not None else ()
    return DividendExecutionPlan(snap, tuple(supplements), taxes)


def inputs():
    dates = pd.bdate_range("2024-01-02", periods=7)
    all_dates = dates.insert(0, pd.Timestamp("2024-01-01"))
    prices = [100, 100, 100, 98, 98, 98, 98, 98]
    bars = pd.DataFrame(dict(date=all_dates, symbol=SYMBOL, raw_open=prices,
                             raw_close=prices, volume=10000, trade_status=1, is_st=0))
    bars["raw_high"], bars["raw_low"] = bars.raw_close+1, bars.raw_close-1
    bars["adj_close"] = 100.0
    targets = pd.DataFrame(dict(date=dates, symbol=SYMBOL, target_weight=[1]+[np.nan]*6))
    return dates, bars, targets


def fees():
    return FeeSchedule([FeeRule("2000-01-01", commission_bps=0, minimum_commission_cny=0, slippage_bps=0)])


def execute(bars, targets, plan, *, cash=10000, first=True, **kwargs):
    return run_target_positions(targets, bars, pd.DatetimeIndex(targets.date),
                                ExecutionBacktestConfig(initial_cash=cash), fees(),
                                dividend_plan=plan, buy_at_first_open=first, **kwargs)


def test_manual_100_shares_record_receivable_payment_and_old_oracle(tmp_path):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    result = execute(bars, targets, plan)
    nav = result["nav"]
    assert result["entitlements"].eligible_shares.tolist() == [100]
    assert result["entitlements"].gross.tolist() == [D(200)]
    assert nav.available_cash.tolist() == [0, 0, 0, 0, 200, 200, 200]
    assert nav.recognized_receivables.tolist() == [0, 0, 200, 200, 0, 0, 0]
    assert nav.portfolio_value.tolist() == [10000]*7
    assert len(result["payments"]) == 1
    assert result["payments"].iloc[0].actual_date == pd.Timestamp("2024-01-08")
    assert result["trades"].execution_price.tolist() == [100]
    oracle = cash_dividend_ledger(nav.assign(raw_close=nav.mark_price, trade_cash_flow=[-10000]+[0]*6),
        plan.snapshot.events, plan.snapshot.coverage, security_id=SYMBOL, initial_cash=10000,
        tax_rates={"cash-1": "0"})["ledger"]
    assert list(map(float, oracle.equity_gross)) == nav.portfolio_value.tolist()
    assert np.allclose(nav.cash + nav.market_value + nav.recognized_receivables, nav.portfolio_value)


@pytest.mark.parametrize("sale_signal,expected", [(0, 0), (1, 100)])
def test_sell_before_record_or_after_record(tmp_path, sale_signal, expected):
    dates, bars, targets = inputs()
    # First-open mandate consumes row zero; use an ordinary next-open entry.
    targets.target_weight = [1, 0, np.nan, np.nan, np.nan, np.nan, np.nan]
    if sale_signal == 0:
        # Move record a day later, so the sale occurs before record close.
        source = artifact(tmp_path, "terms.json", {})
        actions = [event(source, security_id=SYMBOL, revision=2, available_at=KNOWN,
                         record_date="2024-01-04", day="2024-01-05", pay_date="2024-01-08")]
        plan = plan_for(tmp_path, dates, events=actions)
    else:
        plan = plan_for(tmp_path, dates)
    result = execute(bars, targets, plan, first=False)
    assert result["entitlements"].eligible_shares.tolist() == [expected]
    assert result["payments"].gross.tolist() == [expected*2]


def test_ex_date_buy_has_no_old_entitlement(tmp_path):
    dates, bars, targets = inputs()
    targets.target_weight = [np.nan, 1, np.nan, np.nan, np.nan, np.nan, np.nan]
    result = execute(bars, targets, plan_for(tmp_path, dates), first=False)
    assert result["trades"].iloc[0].execution_date == pd.Timestamp("2024-01-04")
    assert result["entitlements"].eligible_shares.tolist() == [0]
    assert result["payments"].gross.tolist() == [0]


def test_unknown_tax_is_restricted_and_no_fake_net_result(tmp_path):
    dates, bars, targets = inputs()
    result = execute(bars, targets, plan_for(tmp_path, dates, rate=None))
    nav = result["nav"]
    assert nav.available_cash.tolist() == [0]*7
    assert nav.restricted_cash.tolist() == [0, 0, 0, 0, 200, 200, 200]
    assert nav.equity_net.iloc[2:].isna().all()
    assert nav.portfolio_value.tolist() == [10000]*7
    assert result["payments"].iloc[0].net is None
    assert result["net_status"] == "UNKNOWN"


@pytest.mark.parametrize("rate,net", [("0", 200), ("0.1", 180), ("1", 0)])
def test_explicit_synthetic_tax_gross_net_bridge(tmp_path, rate, net):
    dates, bars, targets = inputs()
    nav = execute(bars, targets, plan_for(tmp_path, dates, rate=rate))["nav"]
    assert nav.recognized_receivables.iloc[2] == net
    assert nav.available_cash.iloc[-1] == net
    assert nav.portfolio_value.iloc[2] == nav.portfolio_value.iloc[-1] == 9800+net
    assert nav.equity_before_tax_charges.tolist() == [10000]*7


@pytest.mark.parametrize("pay,rate,expected", [("2024-01-05", "0", 200),
    ("2024-01-08", "0", 100), ("2024-01-05", None, 100), ("2024-01-06", "0", 200)])
def test_payment_time_changes_next_order_board_lots(tmp_path, pay, rate, expected):
    dates, bars, targets = inputs()
    # Deliberately constant raw price: isolates funding timing from market P/L.
    bars.loc[:, ["raw_open", "raw_close", "raw_high", "raw_low"]] = 2.0
    source = artifact(tmp_path, "terms.json", {})
    actions = [event(source, security_id=SYMBOL, revision=2, available_at=KNOWN, pay_date=pay)]
    targets.target_weight = [1, 0, np.nan, 1, np.nan, np.nan, np.nan]
    result = execute(bars, targets, plan_for(tmp_path, dates, events=actions, rate=rate), cash=200, first=False)
    buys = result["trades"].loc[result["trades"].side == "buy"]
    assert buys.executed_shares.tolist() == [100, expected]
    assert buys.execution_date.iloc[-1] == pd.Timestamp("2024-01-08")
    if pay == "2024-01-06":
        assert result["payments"].actual_date.iloc[0] == pd.Timestamp(pay)
        assert result["payments"].observed_on.iloc[0] == pd.Timestamp("2024-01-08")


def test_failed_retry_preserves_once_only_entitlement(tmp_path):
    dates, bars, targets = inputs()
    targets.target_weight = [1, 0, np.nan, np.nan, np.nan, np.nan, np.nan]
    bars.loc[bars.date.isin(dates[2:4]), "trade_status"] = 0
    result = execute(bars, targets, plan_for(tmp_path, dates), first=False)
    assert result["rejections"].reason.tolist() == ["suspended", "suspended"]
    assert result["entitlements"].eligible_shares.tolist() == [100]
    assert result["payments"].gross.tolist() == [D(200)]
    assert len(result["trades"]) == 2


@pytest.mark.parametrize("kind", ["BONUS", "CAPITALIZATION", "RIGHTS", "SPLIT", "CONSOLIDATION", "COMPOSITE", "CODE_CHANGE"])
def test_unsupported_actions_fail_explicitly(tmp_path, kind):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    plan = replace(plan, snapshot=replace(plan.snapshot, events=(replace(plan.snapshot.events[0], event_type=kind),)))
    with pytest.raises(DataQualityError, match="UNSUPPORTED"):
        execute(bars, targets, plan)


@pytest.mark.parametrize("change,match", [(dict(record_date=None), "record date"),
    (dict(pay_date=None), "payment date"), (dict(status="PROPOSED"), "implemented"),
    (dict(available_at="2024-01-04T00:00:00+08:00"), "association|availability|unavailable"),
    (dict(source_sha256="0"*64), "source"), (dict(revision=3), "completeness")])
def test_missing_evidence_and_future_revision_refused(tmp_path, change, match):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    plan = replace(plan, snapshot=replace(plan.snapshot, events=(replace(plan.snapshot.events[0], **change),)))
    with pytest.raises(DataQualityError, match=match):
        execute(bars, targets, plan)


def test_complete_label_without_range_evidence_fails(tmp_path):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    with pytest.raises(DataQualityError, match="coverage"):
        execute(bars, targets, replace(plan, snapshot=replace(plan.snapshot, coverage=())))
    fake = replace(plan.snapshot.coverage[0], source_sha256=plan.snapshot.events[0].source_sha256)
    with pytest.raises(DataQualityError, match="completeness"):
        execute(bars, targets, replace(plan, snapshot=replace(plan.snapshot, coverage=(fake,))))


def test_raw_prices_only_and_old_entry_refusals_remain(tmp_path):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    with pytest.raises(SchemaError, match="raw_open"):
        run_target_positions(targets, bars, dates,
            ExecutionBacktestConfig(open_price_column="adj_close"), dividend_plan=plan)


def test_no_event_adapter_is_identical_and_order_perturbation_is_safe(tmp_path):
    dates, bars, targets = inputs()
    old = execute(bars, targets, None)
    new = execute(bars.sample(frac=1, random_state=3), targets, plan_for(tmp_path, dates, events=[]))
    for name in ("nav", "trades", "rejections"):
        pd.testing.assert_frame_equal(old[name], new[name][old[name].columns])
    bad = targets.iloc[::-1]
    with pytest.raises(SchemaError, match="ordered"):
        execute(bars, bad, plan_for(tmp_path, dates))


def test_lot_snapshot_is_immutable_and_fifo_t_plus_one_survives():
    book = PositionBook()
    book.buy(SYMBOL, "2024-01-02", 100)
    book.buy(SYMBOL, "2024-01-03", 200)
    before = book.lot_snapshot(SYMBOL)
    assert book.sellable_shares(SYMBOL, "2024-01-03") == 100
    book.sell(SYMBOL, "2024-01-03", 100)
    assert before == ((pd.Timestamp("2024-01-02"), 100), (pd.Timestamp("2024-01-03"), 200))
    assert book.lot_snapshot(SYMBOL) == ((pd.Timestamp("2024-01-03"), 200),)


def test_dual_ma_and_buy_hold_use_same_integrated_executor(tmp_path):
    dates, bars, targets = inputs()
    warmup = pd.bdate_range("2023-12-26", "2024-01-01")
    prefix = pd.concat([bars.iloc[[0]].assign(date=day) for day in warmup], ignore_index=True)
    bars = pd.concat([prefix, bars.iloc[1:]], ignore_index=True)
    bars["adj_close"] = [100, 100, 100, 100, 100, 90, 110, 110, 90, 90, 110, 110]
    config = DualMAConfig(symbol=SYMBOL, start=str(dates[0].date()), end=str(dates[-1].date()),
                          short_window=1, long_window=2, initial_cash=10000)
    plan = plan_for(tmp_path, dates)
    result = run_integrated_software_backtest(bars, bars.date, plan, config, fee_schedule=fees())
    assert not result["strategy"]["trades"].empty
    assert result["benchmark"]["entitlements"].eligible_shares.tolist() == [100]
    assert set(result["strategy"]["trades"].execution_price) <= {98, 100}
    assert result["financial_admission"] == "FORMAL_ADMISSION_BLOCKED"
    with pytest.raises(SchemaError, match="unsupported"):
        run_software_backtest(bars, bars.date, config, corporate_actions=plan.snapshot.events)
    with pytest.raises(SchemaError, match="variable"):
        run_software_backtest(bars, bars.date, config)


def test_same_day_ex_and_pay_not_spent_at_open(tmp_path):
    dates, bars, targets = inputs()
    result = execute(bars, targets, plan_for(tmp_path, dates, pay="2024-01-04"))
    nav = result["nav"]
    assert nav.opening_available_cash.iloc[2] == 0
    assert nav.available_cash.iloc[2] == 200
    assert nav.opening_available_cash.iloc[3] == 200
    assert result["recognitions"].gross.tolist() == [D(200)]
    assert result["payments"].phase.tolist() == ["AFTER_OPEN_ORDERS"]


def test_payment_beyond_horizon_remains_receivable(tmp_path):
    dates, bars, targets = inputs()
    result = execute(bars, targets, plan_for(tmp_path, dates, pay="2024-02-01"))
    assert result["payments"].empty
    assert result["nav"].recognized_receivables.iloc[-1] == 200
    assert result["nav"].available_cash.iloc[-1] == 0


def test_event_order_and_future_bar_perturbation_do_not_change_past(tmp_path):
    dates, bars, targets = inputs()
    source = artifact(tmp_path, "terms.json", {})
    actions = [event(source, security_id=SYMBOL, revision=2, available_at=KNOWN, event_id="b", pay_date="2024-01-08"),
               event(source, security_id=SYMBOL, revision=2, available_at=KNOWN, event_id="a", pay_date="2024-01-08")]
    plan = plan_for(tmp_path, dates, events=actions)
    first = execute(bars, targets, plan)
    shuffled = replace(plan, snapshot=replace(plan.snapshot, events=plan.snapshot.events[::-1]),
                       supplements=plan.supplements[::-1], taxes=plan.taxes[::-1])
    second = execute(bars.sample(frac=1, random_state=4), targets, shuffled)
    for key in ("nav", "trades", "payments", "entitlements"):
        pd.testing.assert_frame_equal(first[key], second[key])
    changed = bars.copy()
    changed.loc[changed.date == dates[-1], "raw_close"] = 97
    third = execute(changed, targets, plan)
    pd.testing.assert_frame_equal(first["nav"].iloc[:-1], third["nav"].iloc[:-1])


@pytest.mark.parametrize("failure", ["association", "field", "future_field", "coverage_future", "issues", "tax", "duplicate"])
def test_evidence_boundaries_are_not_bypassed(tmp_path, failure):
    dates, bars, targets = inputs()
    plan = plan_for(tmp_path, dates)
    if failure in {"association", "field", "future_field"}:
        import json
        supplement = plan.supplements[0]
        payload = json.loads(supplement.association.read())
        if failure == "association":
            payload["original_sha256"] = "0"*64
        elif failure == "field":
            payload["fields"].pop("record_date")
        else:
            payload["fields"]["record_date"]["available_at"] = "2024-01-04"
        link = artifact(tmp_path, "bad-link.json", payload)
        plan = replace(plan, supplements=(replace(supplement, association=link),),
                       snapshot=replace(plan.snapshot, artifacts=plan.snapshot.artifacts+(link,)))
    elif failure == "coverage_future":
        plan = replace(plan, snapshot=replace(plan.snapshot,
            coverage=(replace(plan.snapshot.coverage[0], available_at="2024-01-02"),)))
    elif failure == "issues":
        plan = replace(plan, snapshot=replace(plan.snapshot, issues=("unresolved cash conflict",)))
    elif failure == "tax":
        plan = replace(plan, taxes=(DividendTax("cash-1", "PERSONAL_INFERRED", "0"),))
    else:
        plan = replace(plan, snapshot=replace(plan.snapshot, events=plan.snapshot.events*2))
    with pytest.raises(DataQualityError):
        execute(bars, targets, plan)


def test_retry_after_cash_arrives_never_double_pays(tmp_path):
    dates, bars, targets = inputs()
    bars.loc[:, ["raw_open", "raw_close", "raw_high", "raw_low"]] = 2.0
    targets.target_weight = [1, 0, np.nan, 1, np.nan, np.nan, np.nan]
    bars.loc[bars.date.isin(dates[4:6]), "trade_status"] = 0
    result = execute(bars, targets, plan_for(tmp_path, dates), cash=200, first=False)
    assert len(result["payments"]) == len(result["entitlements"]) == 1
    assert result["trades"].executed_shares.tolist() == [100, 100, 200]
    assert result["nav"].dividend_cash_flow.sum() == 200
    nav = result["nav"]
    assert np.allclose(nav.available_cash.diff().iloc[1:],
                       (nav.trade_cash_flow+nav.dividend_cash_flow).iloc[1:])
