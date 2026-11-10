import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("statsmodels")

from factorlab.data.hub import DataHub
from factorlab.dataset.identity import file_hash
from factorlab.strategies.pairs_cli import synthetic_panels
from factorlab.strategies.pairs_real_cli import BLOCK, PreparedCsmarAdapter, execute
from factorlab.strategies.pairs_research import _episodes
from factorlab.strategies.pairs_statistics import SelectionConfig, select_pairs
from factorlab.strategies.pairs_trading import PairTradingConfig, read_pair_panels, run_pair_research
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


def research(opens, closes):
    return run_pair_research(opens, closes, selection=SelectionConfig(train_end="2020-01-01"),
        strategy=PairTradingConfig(), splitter=ExpandingWalkForwardSplitter(initial_train_months=12,
        validation_months=3, test_months=3, step_months=3, purge_days=12, embargo_days=12),
        borrow={}, entry_grid=(1.5, 2., 2.5), source_kind="REAL_CANDIDATE", statistics_only=True)


def test_statistical_path_blocks_account_and_freezes_past(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no account may run for statistical-only evidence")
    monkeypatch.setattr("factorlab.strategies.pairs_trading.simulate_pair", forbidden)
    opens, closes = synthetic_panels(periods=600)
    baseline = research(opens, closes)
    cutoff = baseline.signals.date.iloc[len(baseline.signals)//2]
    changed = closes.copy()
    changed.loc[changed.index > cutoff, "A"] *= 1.2
    perturbed = research(opens, changed)
    pd.testing.assert_frame_equal(baseline.signals.loc[baseline.signals.date <= cutoff].reset_index(drop=True),
                                  perturbed.signals.loc[perturbed.signals.date <= cutoff].reset_index(drop=True))
    pd.testing.assert_frame_equal(baseline.selections.loc[baseline.selections.train_end.le(cutoff.isoformat())].reset_index(drop=True),
                                  perturbed.selections.loc[perturbed.selections.train_end.le(cutoff.isoformat())].reset_index(drop=True))
    assert (pd.to_datetime(baseline.folds.train_max_exit_date) < baseline.folds.validation_start).all()
    assert (pd.to_datetime(baseline.folds.validation_max_exit_date) < baseline.folds.test_start).all()
    assert (baseline.signals.next_observed_open_date > baseline.signals.date).all()


def test_correlated_rejection_has_diagnostics_but_no_eligible_signal():
    opens, closes = synthetic_panels(periods=600, relationship="noncointegrated")
    result = research(opens, closes)
    rejected = result.selections.loc[~result.selections.accepted]
    assert not rejected.empty
    assert rejected.price_correlation.max() > .8
    assert rejected.coint_pvalue.notna().all()
    rejected_folds = set(rejected.fold)
    assert not result.signals.loc[result.signals.fold.isin(rejected_folds), "accepted"].any()
    assert result.diagnostics.loc[result.diagnostics.phase.eq("test"), "coint_pvalue"].notna().all()


def test_hedge_rejection_preserves_computable_statistics():
    _, closes = synthetic_panels(periods=500)
    models, table = select_pairs(closes, SelectionConfig(train_end=closes.index[-1].isoformat(),
                                                        max_hedge_ratio_drift=0))
    assert not models
    assert table.iloc[0].reason == "unstable hedge ratio"
    assert table.coint_pvalue.notna().all()
    assert table.adf_pvalue.notna().all()
    negative = closes.copy()
    negative["A"] = 500 - closes.A
    _, table = select_pairs(negative, SelectionConfig(train_end=negative.index[-1].isoformat()))
    assert table.iloc[0].reason == "nonpositive hedge ratio"
    assert table.coint_pvalue.notna().all()


def snapshot(tmp_path, *, duplicate=False):
    opens, closes = synthetic_panels(periods=600)
    symbols = {"A": "600000.SH", "B": "600001.SH"}
    rows = []
    for symbol in opens:
        rows.append(pd.DataFrame(dict(date=closes.index, symbol=symbols[symbol], raw_open=opens[symbol].to_numpy(),
            raw_close=closes[symbol].to_numpy(), adj_close=closes[symbol].to_numpy(), trade_status=1)))
    bars = pd.concat(rows, ignore_index=True)
    bars.loc[0, "trade_status"] = 0
    bars = bars.loc[~((bars.symbol == "600001.SH") & bars.date.eq(closes.index[2]))]
    if duplicate:
        bars = pd.concat([bars, bars.iloc[[1]]])
    root = tmp_path / "private"
    root.mkdir()
    bars.to_parquet(root / "bars.parquet", index=False)
    manifest = dict(schema="P5_R03_CSMAR_PRIVATE_V1", bars_sha256=file_hash(root / "bars.parquet"),
        symbols=list(symbols.values()), references=[], start="2020-01-01", end="2022-12-31",
        price_semantics="SYNTHETIC_INTERFACE_FIXTURE_NOT_REAL_EVIDENCE")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, closes.index


def test_adapter_preserves_dates_gaps_and_suspension(tmp_path):
    root, dates = snapshot(tmp_path)
    adapter = PreparedCsmarAdapter(root)
    opens, closes, receipts = read_pair_panels(DataHub({"fixture": adapter}), "fixture",
        adapter.manifest["symbols"], start="2020-01-01", end="2022-12-31", close_field="adj_close")
    assert pd.isna(closes.loc[dates[0], "600000.SH"])
    assert pd.isna(closes.loc[dates[2], "600001.SH"])
    assert opens.index.equals(closes.index)
    assert receipts[0].metadata.data_version == adapter.manifest["bars_sha256"]


def test_adapter_rejects_duplicates_and_snapshot_corruption(tmp_path):
    root, _ = snapshot(tmp_path, duplicate=True)
    with pytest.raises(ValueError, match="duplicate"):
        PreparedCsmarAdapter(root)
    with (root / "bars.parquet").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        PreparedCsmarAdapter(root)


def test_config_replay_and_execution_gate(tmp_path, monkeypatch):
    root, _ = snapshot(tmp_path)
    config = dict(start="2020-01-01", end="2022-12-31", execution=BLOCK, price_field="adj_close",
        pairs=[dict(symbols=["600000.SH", "600001.SH"], rationale="synthetic interface fixture")],
        strategy={}, selection={}, splitter=dict(initial_train_months=12, validation_months=3,
        test_months=3, step_months=3, purge_days=12, embargo_days=12), entry_grid=[1.5, 2., 2.5],
        correlation_control_threshold=.8, multiple_testing="one synthetic interface candidate")
    def forbidden(*args, **kwargs):
        raise AssertionError("PnL called with unverified actions/borrow")
    monkeypatch.setattr("factorlab.strategies.pairs_trading.simulate_pair", forbidden)
    a, meta = execute(config, candidate=root, output=tmp_path / "run-a")
    import statsmodels

    identity = json.loads((tmp_path / "run-a/identity.json").read_text(encoding="utf-8"))
    assert identity["statsmodels"] == statsmodels.__version__
    saved = json.loads((tmp_path / "run-a/config.json").read_text(encoding="utf-8"))
    b, _ = execute(saved, candidate=root, output=tmp_path / "run-b")
    for key in ("selections", "signals", "summary"):
        pd.testing.assert_frame_equal(a[key], b[key])
    assert meta["pnl_metrics"] is None
    assert not (tmp_path / "run-a/nav.csv").exists()
    config["execution"] = "ALLOW"
    with pytest.raises(ValueError, match="execution blocked"):
        execute(config, candidate=root, output=tmp_path / "must-not-exist")


def test_recovery_censors_missing_data_and_boundary():
    dates = pd.bdate_range("2020-01-01", periods=5)
    frame = pd.DataFrame(dict(date=dates, next_observed_open_date=dates + pd.Timedelta(days=1),
                              zscore=[2.5, np.nan, 2.5, 1.8, 1.7], accepted=True))
    rows = _episodes(frame, 2., PairTradingConfig())
    assert [r["reason"] for r in rows] == ["missing_observation", "fold_boundary"]
    assert all(r["censored"] and not r["recovered"] for r in rows)
