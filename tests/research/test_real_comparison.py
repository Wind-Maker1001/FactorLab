"""Synthetic software checks; actual historical results are separate private artifacts."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from factorlab.data.evidence import sha256
from factorlab.errors import SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.real_comparison import compare, run, NAMES, COMPLETED


@pytest.fixture
def sample():
    configuration = json.loads((Path(__file__).parents[2] / "configs/research/p5-r02-601857-2018.json").read_text())
    dates = pd.bdate_range("2017-09-01", periods=140)
    configuration["warmup_start"] = str(dates[0].date())
    configuration["common"].update(start=str(dates[60].date()), end=str(dates[-1].date()))
    close = 10 + np.sin(np.arange(140) / 5) + np.arange(140) / 200
    bars = pd.DataFrame(dict(date=dates, symbol="601857.SH", raw_open=close + .01,
        raw_close=close, raw_high=close + .1, raw_low=close - .1,
        adj_close=np.nan, volume=1000000, trade_status=True, is_st=False))
    return bars, dates, configuration


def test_shared_execution_costs_and_reconciliation(sample):
    bars, dates, config = sample
    result = compare(bars, dates, config)
    benchmark = result["legs"]["buy_hold"]["nav"]
    assert "adj_close" not in result["input_bars"]
    for name in NAMES:
        leg, stats = result["legs"][name], result["summary"][name]
        nav, trades = leg["nav"], leg["trades"]
        assert nav.date.equals(benchmark.date)
        np.testing.assert_array_equal(nav.benchmark_nav, benchmark.strategy_nav)
        assert nav.shares.mod(100).eq(0).all()
        assert nav.cash.ge(0).all()
        flow = np.where(trades.side.eq("buy"), -trades.trade_notional, trades.trade_notional) - trades.transaction_cost
        daily = trades.assign(flow=flow).groupby("execution_date").flow.sum().reindex(nav.date, fill_value=0)
        np.testing.assert_allclose(nav.cash, config["common"]["initial_cash"] + daily.cumsum())
        share_flow = np.where(trades.side.eq("buy"), trades.executed_shares, -trades.executed_shares)
        shares = trades.assign(flow=share_flow).groupby("execution_date").flow.sum().reindex(nav.date, fill_value=0).cumsum()
        np.testing.assert_array_equal(nav.shares, shares)
        np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.market_value)
        np.testing.assert_allclose(nav.market_value, nav.shares * nav.mark_price)
        np.testing.assert_allclose(np.cumprod(1 + nav.strategy_return), nav.portfolio_value / 100000)
        for t in trades.itertuples():
            assert t.execution_price == bars.set_index("date").loc[t.execution_date, "raw_open"]
            expected = result["fees"].calculate(t.execution_date, t.side, t.trade_notional)
            assert t.transaction_cost == pytest.approx(expected.total)
            if name != "buy_hold":
                assert t.execution_date > t.signal_date
        assert stats["total_transaction_cost"] == pytest.approx(trades.transaction_cost.sum())
        assert stats["cumulative_return"] == pytest.approx(nav.strategy_nav.iloc[-1] - 1)
        assert stats["excess_return"] == pytest.approx(nav.strategy_nav.iloc[-1] - benchmark.strategy_nav.iloc[-1])
        assert stats["relative_buy_hold_return"] == pytest.approx(nav.strategy_nav.iloc[-1] / benchmark.strategy_nav.iloc[-1] - 1)
        assert stats["maximum_drawdown"] == nav.drawdown.min()
        assert stats["total_return"] is None
    # First-open baseline includes fees and intraday price movement, not a first-close rebase.
    assert benchmark.strategy_nav.iloc[0] != 1
    assert result["legs"]["dual_ma"]["trades"].shape[0] > 0


def test_future_prices_do_not_change_past_or_same_open(sample):
    bars, dates, config = sample
    before = compare(bars, dates, config)
    altered = bars.copy()
    altered.loc[100:, ["raw_open", "raw_high", "raw_low", "raw_close"]] *= 1.3
    after = compare(altered, dates, config)
    for name in NAMES:
        assert_frame_equal(before["signals"][name].iloc[:40], after["signals"][name].iloc[:40])
        assert_frame_equal(before["legs"][name]["nav"].iloc[:40], after["legs"][name]["nav"].iloc[:40])
    # Today's closing signal must not affect today's opening fill.
    altered = bars.copy()
    altered.loc[100, ["raw_close", "raw_high"]] *= 1.3
    after = compare(altered, dates, config)
    for name in NAMES:
        a = before["legs"][name]["trades"]
        b = after["legs"][name]["trades"]
        assert_frame_equal(a.loc[a.execution_date <= dates[100]], b.loc[b.execution_date <= dates[100]])


def test_vendor_adjustment_cannot_enter_raw_signals(sample):
    bars, dates, config = sample
    before = compare(bars, dates, config)
    changed = bars.copy()
    changed["adj_close"] = np.arange(len(bars)) ** 2 + 1000
    after = compare(changed, dates, config)
    absent = compare(bars.drop(columns="adj_close"), dates, config)
    for name in NAMES:
        assert "adj_close" not in after["signals"][name]
        assert_frame_equal(before["signals"][name], after["signals"][name])
        assert_frame_equal(before["legs"][name]["nav"], absent["legs"][name]["nav"])
    config["signal_price_column"] = "adj_close"
    with pytest.raises(SchemaError, match="raw_close"):
        compare(bars, dates, config)


def test_suspended_intent_retries_and_terminal_intent_survives(sample):
    bars, dates, config = sample
    bars.loc[60:61, "trade_status"] = False
    result = compare(bars, dates, config)
    hold = result["legs"]["buy_hold"]
    assert hold["rejections"].reason.tolist() == ["suspended", "suspended"]
    assert hold["trades"].execution_date.tolist() == [dates[62]]
    assert hold["nav"].pending_target.iloc[:2].eq(1).all()
    bars.loc[60:, "trade_status"] = False
    result = compare(bars, dates, config)
    assert result["legs"]["buy_hold"]["trades"].empty
    assert result["summary"]["buy_hold"]["terminal_pending_target"] == 1
    assert result["summary"]["buy_hold"]["rejection_count"] == 80
    assert result["summary"]["buy_hold"]["sharpe_ratio"] is None


def test_shared_contract_cannot_be_overridden_and_missing_raw_refused(sample):
    bars, dates, config = sample
    bad = deepcopy(config)
    bad["strategies"]["rsi"]["initial_cash"] = 90000
    with pytest.raises(SchemaError, match="cannot override"):
        compare(bars, dates, bad)
    with pytest.raises(SchemaError, match="required columns"):
        compare(bars.drop(columns="raw_close"), dates, config)
    with pytest.raises(SchemaError, match="every supplied calendar"):
        compare(bars.drop(index=70), dates, config)


def prepared(tmp_path, sample):
    bars, dates, config = sample
    root = tmp_path / "synthetic-contract-only"
    for folder in ("market", "calendar", "corporate_actions", "provenance"):
        (root / folder).mkdir(parents=True)
    bars.to_parquet(root / "market/bars.parquet", index=False)
    pd.DataFrame({"date": dates}).to_parquet(root / "calendar/observed_sessions.parquet", index=False)
    (root / "corporate_actions/events.json").write_text("[]")
    (root / "provenance/references.json").write_text('{"synthetic_test_only":true}')
    manifest = dict(schema="CSMAR_LOCAL_RESEARCH_CANDIDATE_V1", source="CSMAR_ONLY",
        files={p.relative_to(root).as_posix(): sha256(p.read_bytes()) for p in root.rglob("*") if p.is_file()},
        coverage={"action_coverage": "PARTIAL"})
    (root / "manifest.json").write_text(json.dumps(manifest))
    config["manifest_sha256"] = sha256((root / "manifest.json").read_bytes())
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return root, path


def test_saved_input_replay_all_artifacts_and_tracker(tmp_path, sample):
    root, path = prepared(tmp_path, sample)
    first, stats = run(path, root, tmp_path / "first")
    second, replay = run(tmp_path / "first/config.json", root, tmp_path / "second")
    assert first["status"] == second["status"] == COMPLETED
    assert stats == replay
    assert first["artifacts_sha256"] == second["artifacts_sha256"]
    tracker = ExperimentTracker(tmp_path / "first/tracker")
    record = tracker.get_run("comparison")["payload"]["run"]
    assert record["qualification"] == "REAL_CANDIDATE"
    assert record["validation"]["total_return"] == "TOTAL_RETURN_UNAVAILABLE"
    assert first["source"]["coverage"]["action_coverage"] == "PARTIAL"
    (root / "corporate_actions/events.json").write_text("[{}]")
    with pytest.raises(SchemaError, match="payload differs"):
        run(path, root, tmp_path / "drift")
    assert not (tmp_path / "drift").exists()


def test_missing_private_source_records_six_unavailable_statuses(tmp_path, sample):
    _, _, config = sample
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    manifest, stats = run(path, tmp_path / "absent", tmp_path / "blocked")
    assert manifest["status"] == "DATA_UNAVAILABLE"
    assert stats == {}
    saved = json.loads((tmp_path / "blocked/manifest.json").read_text())
    assert set(saved["strategies"]) == set(NAMES)
    assert all(s["status"] == "DATA_UNAVAILABLE" for s in saved["strategies"].values())
