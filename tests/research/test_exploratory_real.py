"""Synthetic hand examples test software contracts, never real-data evidence."""
from dataclasses import asdict
import json

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from factorlab.backtest.fees import FeeSchedule
from factorlab.data.evidence import sha256
from factorlab.data.admission import assert_formal_market_admission
from factorlab.errors import ProviderError, SchemaError
from factorlab.strategies.dual_ma import DualMAConfig
from factorlab.strategies.exploratory_real import load_csmar, run, run_price_research


def sample():
    dates = pd.bdate_range("2018-01-01", periods=9)
    close = np.array([10, 10, 10, 11, 12, 12, 10, 9, 9], dtype=float)
    config = DualMAConfig(start=str(dates[3].date()), end=str(dates[-1].date()),
                          short_window=2, long_window=3, initial_cash=10000)
    bars = pd.DataFrame(dict(date=dates, symbol=config.symbol, raw_open=10.,
        raw_close=close, raw_high=np.maximum(10, close), raw_low=np.minimum(10, close),
        adj_close=np.arange(9) + 100., volume=100000, trade_status=True, is_st=False))
    return bars, dates, config


def assert_ledger(leg, initial):
    nav, trades = leg["nav"], leg["trades"]
    cash_flow = trades.assign(flow=np.where(trades.side.eq("buy"),
        -trades.trade_notional, trades.trade_notional) - trades.transaction_cost)
    flows = cash_flow.groupby("execution_date").flow.sum().reindex(nav.date, fill_value=0)
    np.testing.assert_allclose(nav.cash, initial + flows.cumsum())
    share_flow = trades.assign(flow=np.where(trades.side.eq("buy"),
        trades.executed_shares, -trades.executed_shares))
    shares = share_flow.groupby("execution_date").flow.sum().reindex(nav.date, fill_value=0).cumsum()
    np.testing.assert_allclose(nav.shares, shares)
    np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.shares * nav.mark_price)
    np.testing.assert_allclose(nav.strategy_nav, nav.portfolio_value / initial)


def test_raw_signal_hand_calculation_and_daily_reconciliation():
    bars, dates, config = sample()
    result, raw = run_price_research(bars, dates, config, FeeSchedule.a_share_default())
    assert result["signals"].signal.tolist() == ["buy", "hold", "hold", "sell", "hold", "hold"]
    assert result["strategy"]["trades"].execution_date.tolist() == [dates[4], dates[7]]
    np.testing.assert_allclose(result["strategy"]["nav"].cash, [10000, 990.5, 990.5, 990.5, 9972, 9972])
    assert result["benchmark"]["trades"].execution_date.tolist() == [dates[3]]
    assert result["benchmark"]["nav"].portfolio_value.iloc[-1] == pytest.approx(9090.5)
    assert result["total_return"]["status"] == "UNAVAILABLE"
    with pytest.raises(ProviderError, match="QUALIFICATION REQUIRED"):
        assert_formal_market_admission(result, membership=pd.DataFrame(), calendar=dates,
            bars=raw, benchmark=pd.DataFrame(), research_start=config.start,
            research_end=config.end, require_execution=True)
    assert raw.adj_close.equals(raw.raw_close)
    assert not bars.adj_close.equals(raw.adj_close)
    for name in ("strategy", "benchmark"):
        assert_ledger(result[name], config.initial_cash)
    assert result["strategy"]["nav"].date.equals(result["benchmark"]["nav"].date)


def test_future_and_vendor_adjustments_do_not_change_past():
    bars, dates, config = sample()
    fees = FeeSchedule.a_share_default()
    before, _ = run_price_research(bars, dates, config, fees)
    changed = bars.copy()
    changed["adj_close"] = np.nan
    changed.loc[8, ["raw_close", "raw_high"]] = 15
    after, _ = run_price_research(changed, dates, config, fees)
    assert_frame_equal(before["signals"].iloc[:-1], after["signals"].iloc[:-1])
    assert_frame_equal(before["strategy"]["nav"].iloc[:-1], after["strategy"]["nav"].iloc[:-1])
    assert_frame_equal(before["strategy"]["trades"], after["strategy"]["trades"])


@pytest.mark.parametrize("column", ["raw_close", "raw_open", "trade_status", "is_st"])
def test_missing_required_observation_never_fabricates_fill(column):
    bars, dates, config = sample()
    bars[column] = np.nan
    with pytest.raises(SchemaError):
        run_price_research(bars, dates, config, FeeSchedule.a_share_default())


def test_missing_calendar_row_rejected():
    bars, dates, config = sample()
    with pytest.raises(SchemaError, match="every supplied calendar"):
        run_price_research(bars.drop(index=4), dates, config, FeeSchedule.a_share_default())


def prepared(tmp_path):
    bars, dates, config = sample()
    root = tmp_path / "synthetic-contract-fixture"
    for directory in ("market", "calendar", "corporate_actions", "provenance"):
        (root / directory).mkdir(parents=True)
    bars.to_parquet(root / "market/bars.parquet", index=False)
    pd.DataFrame({"date": dates}).to_parquet(root / "calendar/observed_sessions.parquet", index=False)
    (root / "corporate_actions/events.json").write_text('[{"event":{"effective_date":"2018-01-09","event_type":"CASH_DIVIDEND"}}]')
    (root / "provenance/references.json").write_text('[{"synthetic_test_only":true}]')
    files = {p.relative_to(root).as_posix(): sha256(p.read_bytes()) for p in root.rglob("*") if p.is_file()}
    manifest = dict(schema="CSMAR_LOCAL_RESEARCH_CANDIDATE_V1", source="CSMAR_ONLY", files=files,
                    coverage={"action_coverage": "PARTIAL", "calendar": "OBSERVED_SECURITY_SESSIONS_ONLY"})
    (root / "manifest.json").write_text(json.dumps(manifest))
    definition = dict(source="CSMAR_PREPARED", manifest_sha256=sha256((root / "manifest.json").read_bytes()),
        warmup_start="2018-01-01", strategy=asdict(config), fees={})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(definition))
    return root, path, definition


def test_pinned_configuration_replay_and_artifacts(tmp_path):
    root, path, _ = prepared(tmp_path)
    a, stats = run(path, root, tmp_path / "a")
    b, replay_stats = run(tmp_path / "a/config.json", root, tmp_path / "b")
    assert stats == replay_stats
    for name in ("signals.csv", "strategy_nav.csv", "strategy_trades.csv", "benchmark_nav.csv", "metrics.json"):
        assert a["artifacts_sha256"][name] == b["artifacts_sha256"][name]
    assert a["known_action_candidates"]
    assert a["total_return"]["value"] is None
    assert not a["execution_admission"] and not a["forward_admission"]
    assert a["source"]["coverage"]["action_coverage"] == "PARTIAL"
    assert (tmp_path / "a/tracker/runs/comparison.json").is_file()


def test_pins_reject_manifest_or_consumed_file_drift(tmp_path):
    root, _, definition = prepared(tmp_path)
    with pytest.raises(SchemaError, match="manifest differs"):
        load_csmar(root, "0" * 64)
    (root / "corporate_actions/events.json").write_text("[]")
    with pytest.raises(SchemaError, match="payload differs"):
        load_csmar(root, definition["manifest_sha256"])


def test_flat_strategy_records_undefined_metrics_as_null(tmp_path):
    root, path, definition = prepared(tmp_path)
    definition["strategy"]["short_window"] = 1
    definition["strategy"]["long_window"] = 2
    # Entire selected research interval is bullish; no crossing is carried in.
    definition["strategy"]["start"] = "2018-01-05"
    definition["strategy"]["end"] = "2018-01-08"
    path.write_text(json.dumps(definition))
    _, stats = run(path, root, tmp_path / "flat")
    assert stats["strategy"]["trade_count"] == 0
    assert stats["strategy"]["sharpe_ratio"] is None
