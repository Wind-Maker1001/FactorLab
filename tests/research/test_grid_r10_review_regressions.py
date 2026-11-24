"""Settlement ties and invalid-hour rejection for PR #238's two P2 findings."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import pandas as pd
import pytest

from factorlab.experiments.grid_funding_adaptive import (
    compute_equal_bh_benchmark, compute_funding_overlay_series,
    load_and_audit_r10_dataset, validate_hourly_timeline,
)
from factorlab.experiments.grid_r10_study import (
    build_nonoverlapping_weekly_windows, precompute_ac_multi_regime,
    validate_research_windows,
)

START = datetime(2026, 5, 15, tzinfo=timezone.utc)


def bars(n=168):
    return [{"time": (START + timedelta(hours=i)).isoformat(), "close": 200.0} for i in range(n)]


def overlay(hours, fills, rates, **kwargs):
    dates = [START + timedelta(hours=h) for h in hours]
    nav = pd.DataFrame({"date": dates, "close": 200.0, "equity": 10.0}, index=range(5, 5 + len(hours)))
    trades = pd.DataFrame(fills, columns=["event_time", "side", "executed_shares"])
    events = [(t + timedelta(hours=1, microseconds=-1), D(200), D(str(r)) / 10000, "Regular")
              for t, r in zip(dates, rates)]
    actual = compute_funding_overlay_series(nav, trades, capital=10, **kwargs)
    historical = compute_funding_overlay_series(nav, trades, capital=10, funding_events=events)
    assert actual[0:2] == historical[0:2]
    pd.testing.assert_series_equal(actual[2], historical[2])
    return actual


def close(h):
    return START + timedelta(hours=h + 1, microseconds=-1)


def test_switch_buy_low_sell_first_high_exact_amount():
    # The review's minimal reproduction: entry pays zero; old position pays on SELL.
    fills = [(close(0).isoformat(), "BUY", "0.04"), (close(1).isoformat(), "SELL", "0.04")]
    total, dd, equity = overlay([0, 1], fills, [0.125, 22.9669], regime_switch_bps=(1, 0.125, 22.9669))
    assert total == -0.01837352
    assert equity.iloc[0] == 10
    assert equity.iloc[-1] == 10 - 0.01837352
    assert dd == pytest.approx(0.01837352 / 10)


@pytest.mark.parametrize("fills,expected", [
    ([(close(0), "BUY", "0.04")], -0.01837352),
    ([(close(0) - timedelta(minutes=30), "BUY", "0.01"),
      (close(0), "BUY", "0.03"), (close(1), "SELL", "0.04")], -0.0229669),
    ([(close(0) + timedelta(microseconds=1), "BUY", "0.04")], -0.01837352),
    ([(close(1), "BUY", "0.04")], 0.0),
    ([(close(0) - timedelta(hours=1), "BUY", "0.04"), (close(0), "SELL", "0.04")], -0.01837352),
])
def test_constant_ties_partials_and_boundaries(fills, expected):
    fills = [(t.isoformat(), side, q) for t, side, q in fills]
    total, _, _ = overlay([0, 1], fills, [22.9669] * 2, constant_hourly_bps=22.9669)
    assert total == pytest.approx(expected, abs=1e-12)
    switched = overlay([0, 1], fills, [22.9669] * 2, regime_switch_bps=(1, 22.9669, 22.9669))
    assert switched[0] == total


def test_irregular_nav_uses_actual_fills_and_zero_rate():
    fills = [(close(0).isoformat(), "BUY", "0.01"),
             ((close(1) + timedelta(minutes=30)).isoformat(), "BUY", "0.03"),
             (close(4).isoformat(), "SELL", "0.02")]
    # No fabricated settlements for missing NAV observations; no inventory shift.
    total, _, _ = overlay([0, 1, 4, 5], fills, [22.9669] * 4, constant_hourly_bps=22.9669)
    assert total == pytest.approx(-0.03215366)
    total, dd, equity = overlay([0, 1, 4, 5], fills, [0] * 4, constant_hourly_bps=0)
    assert total == dd == 0
    assert equity.eq(10).all()


def test_funding_reconciles_partial_fifo_fragments_and_historical_scale():
    from factorlab.experiments.grid_funding import fragment_funding
    early = close(0) - timedelta(minutes=30)
    fills = [(early.isoformat(), "BUY", "0.01"), (close(0).isoformat(), "BUY", "0.03"),
             (close(1).isoformat(), "SELL", "0.02"), (close(3).isoformat(), "SELL", "0.02")]
    total, _, _ = overlay([0, 1, 2, 3], fills, [22.9669] * 4, constant_hourly_bps=22.9669)
    events = [(close(i), D(200), D("0.00229669"), "Regular") for i in range(4)]
    fragments = [(D("0.01"), early, close(1)), (D("0.01"), close(0), close(1)),
                 (D("0.02"), close(0), close(3))]
    fifo_total = sum(fragment_funding(q, buy, sell, events) for q, buy, sell in fragments)
    assert fifo_total == D("-0.04134042")
    assert total == float(fifo_total)
    nav = pd.DataFrame({"date": [START + timedelta(hours=i) for i in range(4)], "equity": 10})
    trades = pd.DataFrame(fills, columns=["event_time", "side", "executed_shares"])
    scaled, _, _ = compute_funding_overlay_series(nav, trades, capital=10, funding_events=events, funding_scale=2)
    assert scaled == 2 * total


def test_bh_first_close_entry_uses_same_tie_rule():
    b = bars(2)
    historical = [(close(i), D(200), D("0.00229669"), "Regular") for i in range(2)]
    for kwargs in ({"constant_hourly_bps": 22.9669}, {"regime_switch_bps": (1, 22.9669, 22.9669)},
                   {"funding_events": historical}):
        result = compute_equal_bh_benchmark(b, **kwargs)
        assert result["bh_funding_pnl_usdt"] == pytest.approx(-0.01837352)


@pytest.mark.parametrize("times", [[], ["2026-05-15"], ["2026-05-15T00:30:00Z"], [pd.NaT]])
def test_timeline_rejects_empty_naive_off_hour_and_nat(times):
    with pytest.raises(ValueError):
        validate_hourly_timeline(times)


@pytest.mark.parametrize("damage,message", [("inside", "Missing"), ("boundary", "Missing"),
                                            ("duplicate", "Duplicate"), ("order", "increasing")])
def test_weekly_windows_reject_invalid_hours(damage, message):
    b = bars(336)
    if damage in {"inside", "boundary"}:
        del b[80 if damage == "inside" else 168]
    elif damage == "duplicate":
        b.insert(80, b[80])
    else:
        b[80], b[81] = b[81], b[80]
    with pytest.raises(ValueError, match=message):
        build_nonoverlapping_weekly_windows(b, first_start=START)


def test_168_hours_and_six_walk_forward_boundaries(monkeypatch):
    one = build_nonoverlapping_weekly_windows(bars(), first_start=START)
    assert len(one) == 1 and len(one[0]["bars"]) == 168
    assert one[0]["end_exclusive_utc"] == (START + timedelta(hours=168)).isoformat()
    b = bars(27 * 168)
    w = build_nonoverlapping_weekly_windows(b, first_start=START)
    validate_research_windows(b, w)
    # Inspect actual production fold construction, bypass only the Paper runs.
    monkeypatch.setattr("factorlab.experiments.grid_r10_study._window_multi_overlay_worker",
                        lambda t: (t[3], t[2]["window_id"], {}))
    result = precompute_ac_multi_regime(b, w, [], [], [("test", 1, None, None)], max_workers=1)
    tested = set()
    assert len(result["fold_specs"]) == 6
    for _, train_end, test_end, _ in result["fold_specs"]:
        train = {x["time"] for win in w[:train_end] for x in win["bars"]}
        test = {x["time"] for win in w[train_end:test_end] for x in win["bars"]}
        assert train.isdisjoint(test) and tested.isdisjoint(test)
        assert max(train) < min(test)
        assert w[train_end - 1]["end_exclusive_utc"] == w[train_end]["start_utc"]
        tested.update(test)
    assert len(tested) == 18 * 168
    w[1]["bars"][0] = w[0]["bars"][-1]
    with pytest.raises(ValueError):
        validate_research_windows(b, w)


@pytest.mark.parametrize("damage", ["inside", "boundary", "duplicate", "order"])
def test_dataset_audit_rejects_corrupted_cache_before_research(tmp_path, monkeypatch, damage):
    from factorlab.experiments import grid_funding_adaptive as g
    start = datetime(2026, 8, 12, tzinfo=timezone.utc)
    times = list(pd.date_range(start, "2026-11-23T00:00:00+00:00", inclusive="left", freq="h"))
    if damage in {"inside", "boundary"}:
        del times[80 if damage == "inside" else 0]
    elif damage == "duplicate":
        times.insert(80, times[80])
    else:
        times[80], times[81] = times[81], times[80]
    pd.DataFrame({"open_time": times}).to_parquet(tmp_path / "nvdausdt_1h_r07_post_20260812_20261123.parquet")
    root = tmp_path / "docs" / "validation" / "p7-r09"
    root.mkdir(parents=True)
    for name in ("R04B", "R07"):
        pd.DataFrame([{"time": start.isoformat(), "mark_price": 200, "rate": 0, "rate_type": "Regular"}]).to_csv(
            root / f"{name}-base-baseline-funding-settlements.csv", index=False)
    monkeypatch.setattr(g, "load_hourly", lambda _: ([], {}))
    monkeypatch.setattr(g, "_http_get_json", lambda _: pytest.fail("Offline audit must not fetch data"))
    with pytest.raises(ValueError, match="hourly|Hourly"):
        load_and_audit_r10_dataset(repo_root=tmp_path, r04b_snapshot=tmp_path, cache_dir=tmp_path,
                                  allow_network_supplement=False)
