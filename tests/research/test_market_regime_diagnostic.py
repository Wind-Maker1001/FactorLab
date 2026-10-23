"""Offline checks; all test prices and business-day sessions are artificial."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from examples.research import diagnose_market_regime as example
from factorlab.config import PathsConfig, load_config
from factorlab.data.cache import ParquetBarCache
from factorlab.data.schema import canonicalize_bars
from factorlab.diagnostics.market_regime import DIAGNOSTIC_REGIMES, diagnose_market_regime
from factorlab.errors import SchemaError
from factorlab.market.regime import build_market_regime_features
from factorlab.reports.market_regime import _build_history_figure, _build_statistics_figure, write_market_regime_report


def benchmark(prices):
    return pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=len(prices)),
                         "symbol": "000905.SH", "adj_close": np.asarray(prices, dtype=float)})


def diagnose(frame, start=None, end=None, **kwargs):
    return diagnose_market_regime(frame, start if start is not None else frame["date"].min(),
                                 end if end is not None else frame["date"].max(), **kwargs)


@pytest.mark.parametrize("prices,expected", [(np.arange(100, 200), "BULL"),
                                            (np.arange(200, 100, -1), "BEAR"),
                                            (np.full(100, 100), "SIDEWAYS")])
def test_frozen_rules_and_unknown_overlay_do_not_change_formal_states(prices, expected):
    frame = benchmark(prices)
    result = diagnose(frame)
    assert result.daily.iloc[-1]["DIAGNOSTIC_REGIME"] == expected
    assert result.daily.iloc[:59]["DIAGNOSTIC_REGIME"].eq("UNKNOWN").all()
    assert result.daily.iloc[:59]["unknown_reason"].eq("INSUFFICIENT_HISTORY").all()
    assert result.daily.iloc[:59]["MARKET_REGIME"].eq("SIDEWAYS").all()
    pd.testing.assert_series_equal(result.daily["MARKET_REGIME"], build_market_regime_features(frame)["MARKET_REGIME"])
    assert result.coverage["known_sessions"] == 41


def test_mixed_conditions_use_sideways_and_strict_equality_is_not_bull():
    frame = benchmark(np.full(60, 100.0))
    frame.loc[59, "adj_close"] = 200.0
    result = diagnose(frame)
    assert result.daily.iloc[-1]["DIAGNOSTIC_REGIME"] == "BULL"
    frame.loc[40:58, "adj_close"] = 1.0
    assert diagnose(frame).daily.iloc[-1]["DIAGNOSTIC_REGIME"] == "SIDEWAYS"
    frame = benchmark(np.full(60, 100.0))
    assert diagnose(frame).daily.iloc[-1]["DIAGNOSTIC_REGIME"] == "SIDEWAYS"


def test_missing_price_invalidates_full_window_and_does_not_fill_returns():
    frame = benchmark(np.arange(100, 260))
    frame.loc[70, "adj_close"] = np.nan
    result = diagnose(frame)
    assert result.daily.loc[70:129, "DIAGNOSTIC_REGIME"].eq("UNKNOWN").all()
    assert result.daily.loc[70, "unknown_reason"] == "MISSING_PRICE"
    assert result.daily.loc[71:129, "unknown_reason"].eq("INCOMPLETE_MA_WINDOW").all()
    assert result.daily.loc[130, "DIAGNOSTIC_REGIME"] == "BULL"
    assert result.daily.loc[70:71, "daily_return"].isna().all()
    assert result.coverage["state_switches"] == 0
    assert result.coverage["unknown_boundaries"] == 3
    bull_spells = result.spells.loc[result.spells["regime"].eq("BULL")]
    assert bull_spells["sessions"].tolist() == [11, 30]
    assert bull_spells["left_censored"].all() and bull_spells["right_censored"].all()


def test_absent_session_requires_explicit_actual_calendar_not_weekday_guess():
    frame = benchmark(np.arange(100, 260))
    calendar = pd.DatetimeIndex(frame["date"])
    missing = frame.drop(index=70).reset_index(drop=True)
    result = diagnose(missing, calendar=calendar)
    assert len(result.daily) == 160
    assert result.daily.loc[70, "unknown_reason"] == "MISSING_SESSION"
    assert result.coverage["source_rows"] == 159
    assert result.coverage["source_row_coverage"] == 159 / 160
    assert result.daily.loc[70:129, "DIAGNOSTIC_REGIME"].eq("UNKNOWN").all()
    observed = diagnose(missing)
    assert len(observed.daily) == 159
    assert observed.coverage["independent_calendar_verified"] is False
    assert observed.coverage["calendar_basis"] == "observed_benchmark_cache_dates"


@pytest.mark.parametrize("mutation", [
    lambda f: f.iloc[::-1].reset_index(drop=True),
    lambda f: pd.concat([f, f.iloc[[0]]], ignore_index=True),
    lambda f: f.assign(adj_close=np.inf),
    lambda f: f.assign(adj_close=-1),
    lambda f: f.assign(symbol="000300.SH"),
    lambda f: f.assign(date=f["date"].dt.tz_localize("Asia/Shanghai")),
    lambda f: f.assign(date=f["date"] + pd.Timedelta(hours=1)),
    lambda f: f.drop(columns="adj_close"),
])
def test_invalid_dates_and_schema_fail_without_silent_sort_or_price_fallback(mutation):
    with pytest.raises(SchemaError):
        diagnose(mutation(benchmark(np.arange(100, 200))))


@pytest.mark.parametrize("calendar_mutation", [lambda d: d[::-1], lambda d: d.append(d[:1]),
                                               lambda d: d.tz_localize("UTC"), lambda d: d + pd.Timedelta(hours=1)])
def test_bad_independent_calendar_rejected(calendar_mutation):
    frame = benchmark(np.arange(100, 200))
    with pytest.raises(SchemaError, match="calendar"):
        diagnose(frame, calendar=calendar_mutation(pd.DatetimeIndex(frame["date"])))


def test_sorted_string_dates_normalize_and_required_boundaries_are_not_truncated():
    frame = benchmark(np.arange(100, 200))
    text = frame.assign(date=frame["date"].dt.strftime("%Y-%m-%d"))
    pd.testing.assert_frame_equal(diagnose(text).daily, diagnose(frame).daily)
    with pytest.raises(SchemaError, match="boundaries"):
        diagnose(frame, end=frame["date"].max() + pd.Timedelta(days=3))
    with pytest.raises(SchemaError, match="start"):
        diagnose(frame, start=frame["date"].max(), end=frame["date"].min())


def test_future_price_and_missingness_perturbation_preserves_entire_historical_daily_prefix():
    frame = benchmark(5000 * np.exp(np.cumsum(np.random.default_rng(42).normal(0, 0.01, 200))))
    baseline = diagnose(frame)
    changed = frame.copy()
    changed.loc[120:, "adj_close"] *= 1e6
    changed.loc[150, "adj_close"] = np.nan
    perturbed = diagnose(changed)
    pd.testing.assert_frame_equal(baseline.daily.iloc[:120], perturbed.daily.iloc[:120], check_exact=True)
    # A report ending at the cutoff is entirely unchanged, including summaries.
    before, after = diagnose(frame, end=frame.loc[119, "date"]), diagnose(changed, end=frame.loc[119, "date"])
    for name in ("daily", "summary", "spells", "annual", "transitions"):
        pd.testing.assert_frame_equal(getattr(before, name), getattr(after, name), check_exact=True)
    assert before.coverage == after.coverage


def test_prehistory_used_before_report_slice_and_conditioned_returns_are_same_day():
    frame = benchmark(np.linspace(100, 200, 100))
    result = diagnose(frame, start=frame.loc[70, "date"])
    assert result.coverage["warmup_sessions"] == 70
    assert result.daily["DIAGNOSTIC_REGIME"].eq("BULL").all()
    expected = frame["adj_close"].pct_change(fill_method=None).iloc[70:]
    row = result.summary.set_index("regime").loc["BULL"]
    assert row["return_samples"] == len(expected)
    assert row["mean_daily_return"] == pytest.approx(expected.mean())
    assert row["daily_volatility"] == pytest.approx(expected.std(ddof=1))
    assert row["annualized_volatility_252"] == pytest.approx(expected.std(ddof=1) * np.sqrt(252))
    assert result.daily.iloc[0]["daily_return"] == pytest.approx(expected.iloc[0])
    assert result.spells.iloc[0]["left_censored"] and result.spells.iloc[0]["right_censored"]
    assert row["spells"] == 1 and row["max_spell_sessions"] == 30


def test_all_missing_and_one_sample_have_explicit_unknown_and_unestimable_volatility():
    result = diagnose(benchmark([np.nan] * 100))
    assert result.coverage["price_coverage"] == result.coverage["state_coverage"] == 0
    assert result.daily["DIAGNOSTIC_REGIME"].eq("UNKNOWN").all()
    assert result.summary["daily_volatility"].isna().all()
    result = diagnose(benchmark([100, 101]))
    assert result.summary.set_index("regime").loc["UNKNOWN", "return_samples"] == 1
    assert pd.isna(result.summary.set_index("regime").loc["UNKNOWN", "daily_volatility"])


def test_annual_shares_counts_transitions_and_spells_match_daily_rows():
    frame = example.synthetic_benchmark("2021-09-13", "2026-09-11", 42)
    result = diagnose(frame, start="2021-09-13")
    assert result.summary["sessions"].sum() == result.spells["sessions"].sum() == len(result.daily)
    assert result.summary["share"].sum() == pytest.approx(1)
    assert np.allclose(result.annual.groupby("year")["share"].sum(), 1)
    assert result.annual.groupby("year")["sessions"].sum().to_dict() == result.daily.groupby(result.daily["date"].dt.year).size().to_dict()
    direct_switches = sum(a != b and a != "UNKNOWN" and b != "UNKNOWN"
                          for a, b in zip(result.daily["DIAGNOSTIC_REGIME"], result.daily["DIAGNOSTIC_REGIME"].iloc[1:]))
    assert result.coverage["state_switches"] == direct_switches


def test_csv_history_background_statistics_and_png_metadata_agree(tmp_path):
    frame = benchmark(np.arange(100, 280))
    frame.loc[100, "adj_close"] = np.nan
    result = diagnose(frame)
    outputs = write_market_regime_report(result, tmp_path, synthetic=True)
    csv = pd.read_csv(outputs["daily.csv"], parse_dates=["date"])
    assert csv["dataset_kind"].eq("SYNTHETIC_SOFTWARE_CHECK").all()
    assert csv["DIAGNOSTIC_REGIME"].tolist() == result.daily["DIAGNOSTIC_REGIME"].tolist()
    figure = _build_history_figure(result, synthetic=True)
    try:
        axis = figure.axes[0]
        np.testing.assert_equal(axis.lines[0].get_ydata(), csv["adj_close"].to_numpy())
        np.testing.assert_equal(axis.lines[0].get_xdata(), csv["date"].to_numpy())
        assert len(axis.patches) == len(result.spells)
        for patch, spell in zip(axis.patches, result.spells.itertuples()):
            assert patch.get_gid() == "spell-%d-%s" % (spell.spell_id, spell.regime)
            # axvspan is a Rectangle in newer Matplotlib and a Polygon in
            # older versions. Compare the same data-coordinate bounds.
            x = patch.get_path().transformed(patch.get_patch_transform()).vertices[:, 0]
            assert x.min() == pytest.approx(mdates.date2num(spell.start))
            assert x.max() == pytest.approx(mdates.date2num(spell.plot_end))
        assert "NOT predictive returns" in axis.get_title()
    finally:
        plt.close(figure)
    figure = _build_statistics_figure(result, synthetic=True)
    try:
        np.testing.assert_allclose([patch.get_height() for patch in figure.axes[0].patches], result.summary["share"] * 100)
        np.testing.assert_allclose([patch.get_height() for patch in figure.axes[4].patches], result.summary["mean_daily_return"] * 100)
        np.testing.assert_allclose([patch.get_height() for patch in figure.axes[5].patches], result.summary["annualized_volatility_252"] * 100)
        np.testing.assert_equal(figure.axes[3].images[0].get_array().sum(), result.transitions["count"].sum())
    finally:
        plt.close(figure)
    from PIL import Image
    for name in ("market_regime_history.png", "market_regime_statistics.png"):
        with Image.open(outputs[name]) as image:
            assert image.width >= 2000
            assert "SYNTHETIC" in image.info["Description"]
            assert "NOT predictive returns" in image.info["Description"]
    assert "不是预测收益" in outputs["report.md"].read_text(encoding="utf-8")
    original = {name: path.read_bytes() for name, path in outputs.items()}
    with pytest.raises(FileExistsError):
        write_market_regime_report(result, tmp_path)
    assert original == {name: path.read_bytes() for name, path in outputs.items()}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    original = load_config("configs/research/csi500_5y.yaml")
    config = replace(original, paths=PathsConfig(data_root=tmp_path / "data", results_root=tmp_path / "results"))
    object.__setattr__(config, "repo_root", original.repo_root)
    object.__setattr__(config, "config_path", original.config_path)
    monkeypatch.setattr(example, "load_config", lambda path: config)
    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostic must never connect to network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return config


def latest(config):
    paths = sorted((config.paths.results_root / "diagnostics" / "csi500-market-regime").glob("*/manifest.json"))
    return paths[-1], json.loads(paths[-1].read_text(encoding="utf-8"))


def cache_fixture(config):
    # Test fixture only; never install these prices into the user's real cache.
    frame = example.synthetic_benchmark(config.data.start, config.data.end, config.seed)
    frame = frame.assign(open=frame["adj_close"], high=frame["adj_close"], low=frame["adj_close"],
                         close=frame["adj_close"], volume=10000.0, amount=1e8, turnover=0.01, adj_factor=1.0)
    frame = canonicalize_bars(frame)
    cache = ParquetBarCache(config.paths.benchmark_dir)
    cache.update("000905.SH", frame, frame["date"].min(), frame["date"].max(), "FixtureProvider", "explicit-synthetic-fixture-v1")
    return cache


def test_missing_real_cache_fails_and_persists_receipt_with_no_fake_plot(cli):
    assert example.main([]) == 1
    path, payload = latest(cli)
    assert payload["status"] == "failed" and payload["data"]["state"] == "missing"
    assert "REAL CSI500 CACHE REQUIRED" in payload["failures"][0]["message"]
    assert payload["synthetic_only"] is False and payload["elapsed_seconds"] >= 0
    assert not list(path.parent.glob("*.png"))
    assert not cli.paths.data_root.exists()


@pytest.mark.parametrize("synthetic", [False, True])
def test_runner_snapshot_hashes_configuration_and_no_network_no_overwrite(cli, synthetic):
    if not synthetic:
        cache = cache_fixture(cli)
        source_before = {p.name: p.read_bytes() for p in cache.root.iterdir() if p.is_file()}
    assert example.main(["--synthetic"] if synthetic else []) == 0
    path, payload = latest(cli)
    assert payload["status"] == "success" and not payload["failures"]
    assert payload["config"] == cli.to_dict() and payload["git_commit"]
    assert payload["python_version"].startswith("3.") and payload["source_sha256"]
    assert payload["synthetic_only"] is synthetic and payload["network_requests"] == 0
    assert payload["research_conclusions_permitted"] is payload["forward_evidence"] is payload["live_trading"] is False
    assert payload["completed_at"] and payload["elapsed_seconds"] > 0
    assert len(payload["data"]["sha256"]) == len(payload["config_sha256"]) == 64
    for name, evidence in payload["outputs"].items():
        assert hashlib.sha256((path.parent / name).read_bytes()).hexdigest() == evidence["sha256"]
    first = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert example.main(["--synthetic"] if synthetic else []) == 0
    assert latest(cli)[0] != path
    assert first == {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert not cli.paths.runs_dir.exists()
    if not synthetic:
        assert source_before == {p.name: p.read_bytes() for p in cache.root.iterdir() if p.is_file()}
        assert payload["data"]["sha256"] == cache.read_metadata("000905.SH")["content_sha256"]


def test_corrupt_cache_fails_without_network_or_synthetic_fallback(cli):
    cache = cache_fixture(cli)
    cache.data_path("000905.SH").write_bytes(b"corrupted fixture")
    assert example.main([]) == 1
    path, payload = latest(cli)
    assert payload["status"] == "failed"
    assert "hash mismatch" in payload["failures"][0]["message"]
    assert not list(path.parent.glob("*.png"))


def test_report_failure_and_run_collision_preserve_evidence(cli, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected report failure")
    monkeypatch.setattr(example, "write_market_regime_report", fail)
    monkeypatch.setattr(example, "_run_id", lambda: "fixed-run-collision")
    assert example.main(["--synthetic"]) == 1
    path, payload = latest(cli)
    assert payload["status"] == "failed" and payload["data"]["state"] == "synthetic"
    assert payload["failures"][0]["message"] == "injected report failure"
    first = path.read_bytes()
    assert example.main(["--synthetic"]) == 1
    assert path.read_bytes() == first
