from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("statsmodels", reason="install factorlab[pairs] for cointegration research")

from factorlab.backtest.pairs import BorrowTerms
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.pairs_cli import execute, synthetic_panels
from factorlab.strategies.pairs_statistics import SelectionConfig, fit_pair, select_pairs
from factorlab.strategies.pairs_trading import (
    PairTradingConfig, mean_reversion_signal, read_pair_panels, run_pair_research, simulate_pair,
)
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


BORROW = {s: BorrowTerms(100000, .08) for s in ("A", "B")}


def setup_case():
    opens, closes = synthetic_panels(periods=500)
    model = fit_pair(closes, SelectionConfig(train_end=closes.index[249].isoformat()))
    dates = closes.index[270:340]
    closes.loc[dates[0], "A"] += 15
    opens.loc[dates[1], "A"] += 15
    cfg = PairTradingConfig(entry_z=5, diagnostic_interval=100, failure_pvalue=1., max_loss_fraction=.5)
    return opens, closes, model, dates, cfg


def test_zscore_entries_exits_and_no_same_decision_reentry():
    cfg = PairTradingConfig()
    assert mean_reversion_signal(2.1, 0, cfg) == ("enter", -1)
    assert mean_reversion_signal(-2.1, 0, cfg) == ("enter", 1)
    assert mean_reversion_signal(.3, -1, cfg) == ("exit", 0)
    assert mean_reversion_signal(-1, -1, cfg) == ("exit", 0)
    assert mean_reversion_signal(-1, 1, cfg) == ("hold", 1)
    assert mean_reversion_signal(float("nan"), 1, cfg) == ("hold", 1)


def test_next_open_two_legs_and_account_reconciliation():
    opens, closes, model, dates, cfg = setup_case()
    result = simulate_pair(opens, closes, dates, model, cfg, BORROW)
    first = result.trades.iloc[:2]
    assert set(first.action) == {"short", "buy"}
    assert first.execution_date.eq(dates[1]).all()
    assert first.decision_date.eq(dates[0]).all()
    assert first.iloc[0].price == opens.loc[dates[1], first.iloc[0].symbol]
    assert (result.trades.execution_date > result.trades.decision_date).all()
    for day, trades in result.trades.groupby("execution_date"):
        assert not ({"short", "buy"} & set(trades.action) and {"cover", "sell"} & set(trades.action))
    nav = result.nav
    np.testing.assert_allclose(nav.equity, nav.cash + nav.long_value - nav.short_liability)
    np.testing.assert_allclose(nav.equity, cfg.initial_cash + nav.long_pnl + nav.short_pnl
                               - nav.cumulative_transaction_cost - nav.cumulative_borrow_cost)
    assert nav.gross_exposure.iloc[-1] == 0
    assert nav.borrow_cost.sum() > 0
    assert nav.transaction_cost.sum() == pytest.approx(result.trades.transaction_cost.sum())


def test_log_model_hedge_maps_to_entry_price_notional_elasticity():
    opens, closes = synthetic_panels(periods=500)
    opens, closes = np.exp(opens / 100), np.exp(closes / 100)
    model = fit_pair(closes, SelectionConfig(train_end=closes.index[249].isoformat(), price_transform="log"))
    dates = closes.index[270:340]
    closes.loc[dates[0], "A"] *= np.exp(.15)
    opens.loc[dates[1], "A"] *= np.exp(.15)
    cfg = PairTradingConfig(entry_z=5, diagnostic_interval=100, failure_pvalue=1., max_loss_fraction=.5)
    result = simulate_pair(opens, closes, dates, model, cfg, BORROW)
    entry = result.trades.iloc[:2].set_index("symbol")
    effective_beta = model.hedge_ratio * opens.loc[dates[1], "A"] / opens.loc[dates[1], "B"]
    assert entry.loc["B", "shares"] == int(entry.loc["A", "shares"] * effective_beta)
    assert entry.loc["B", "shares"] != int(entry.loc["A", "shares"] * model.hedge_ratio)


def test_future_perturbations_do_not_change_past_decisions_or_fills():
    opens, closes, model, dates, cfg = setup_case()
    cutoff = dates[25]
    baseline = simulate_pair(opens, closes, dates, model, cfg, BORROW)
    modified_opens, modified_closes = opens.copy(), closes.copy()
    modified_opens.loc[modified_opens.index > cutoff] *= 1.02
    modified_closes.loc[modified_closes.index > cutoff] *= 1.02
    changed = simulate_pair(modified_opens, modified_closes, dates, model, cfg, BORROW)
    for key in ("signals", "nav", "positions"):
        left, right = getattr(baseline, key), getattr(changed, key)
        pd.testing.assert_frame_equal(left[left.date <= cutoff].reset_index(drop=True),
                                      right[right.date <= cutoff].reset_index(drop=True))
    pd.testing.assert_frame_equal(baseline.trades[baseline.trades.execution_date <= cutoff].reset_index(drop=True),
                                  changed.trades[changed.trades.execution_date <= cutoff].reset_index(drop=True))


def test_single_leg_failure_exit_uses_next_open_and_reports_exposure():
    opens, closes, model, dates, cfg = setup_case()
    # Positive spread shorts A first, then attempts B long.
    result = simulate_pair(opens, closes, dates, model, cfg, BORROW,
                           fill_scenarios={dates[1].strftime("%Y-%m-%d"): {"B": 0}})
    assert result.failures.iloc[0].reason == "NO_FILL"
    assert result.trades.iloc[0].symbol == "A"
    assert result.trades.iloc[0].action == "short"
    assert result.trades.iloc[1].action == "cover"
    assert result.trades.iloc[1].execution_date == dates[2]
    assert result.trades.iloc[1].reason == "leg_failure"
    assert result.signals.iloc[1].unresolved_exposure
    assert result.nav.iloc[1].long_value == 0
    assert result.nav.iloc[1].short_liability > 0


@pytest.mark.parametrize("risk", ["cointegration_failure", "max_loss", "max_holding", "margin_call", "gross_limit"])
def test_risk_exit_is_decided_before_next_open(monkeypatch, risk):
    opens, closes, model, dates, cfg = setup_case()
    if risk == "cointegration_failure":
        calls = iter([(-5., .001, None), (-1., .9, None)])
        monkeypatch.setattr("factorlab.strategies.pairs_trading.coint", lambda *a, **k: next(calls, (-1., .9, None)))
        cfg = replace(cfg, diagnostic_interval=2, failure_pvalue=.05)
    elif risk == "max_loss":
        # Short A loses on the entry day's close; the threshold is on account equity.
        closes.loc[dates[1], "A"] += 100
        cfg = replace(cfg, max_loss_fraction=.001)
    elif risk == "max_holding":
        closes.loc[dates[1:5], "A"] += 12
        cfg = replace(cfg, max_holding_sessions=2)
    elif risk == "gross_limit":
        cfg = replace(cfg, max_gross_leverage=.51)
        closes.loc[dates[1], "A"] += 100
    else:
        cfg = replace(cfg, margin_rate=3., gross_fraction=.3)
        closes.loc[dates[1], "A"] += 200
    result = simulate_pair(opens, closes, dates, model, cfg, BORROW)
    exits = result.trades[result.trades.reason == risk]
    assert not exits.empty, result.signals[["date", "reason"]].to_string()
    assert (exits.execution_date > exits.decision_date).all()
    if risk == "max_holding":
        assert dates.get_loc(exits.execution_date.iloc[0]) - 1 == cfg.max_holding_sessions


def test_window_end_failed_cover_remains_in_final_risk_receipts():
    opens, closes, model, dates, cfg = setup_case()
    dates = dates[:3]
    result = simulate_pair(opens, closes, dates, model, cfg, BORROW,
                           fill_scenarios={dates[-1].strftime("%Y-%m-%d"): {"A": 0}})
    assert result.nav.gross_exposure.iloc[-1] > 0
    assert result.signals.unresolved_exposure.iloc[-1]
    assert result.failures.iloc[-1].reason == "NO_FILL"
    assert result.positions[result.positions.date == dates[-1]].side.tolist() == ["short"]


def test_walk_forward_isolation_and_purge_embargo():
    opens, closes = synthetic_panels(periods=600)
    cfg = PairTradingConfig(diagnostic_window=60)
    splitter = ExpandingWalkForwardSplitter(initial_train_months=12, validation_months=3,
        test_months=3, step_months=3, purge_days=5, embargo_days=5)
    selection = SelectionConfig(train_end="2020-01-01")
    result = run_pair_research(opens, closes, selection=selection, strategy=cfg,
                               splitter=splitter, borrow=BORROW, entry_grid=(2.,))
    assert not result.trades.empty
    for fold in result.folds.itertuples():
        assert pd.Timestamp(fold.train_end) < fold.validation_start < fold.validation_end < fold.test_start
        assert fold.model["train_end"] == fold.train_end
        assert fold.purged_sessions == 5
        assert fold.embargoed_sessions == 5
        assert fold.train_overlap_rows_removed > 0
        assert fold.validation_overlap_rows_removed > 0
    cutoff = result.nav[result.nav.fold == 0].date.iloc[20]
    changed_opens, changed_closes = opens.copy(), closes.copy()
    changed_opens.loc[changed_opens.index > cutoff] *= 1.01
    changed_closes.loc[changed_closes.index > cutoff] *= 1.01
    changed = run_pair_research(changed_opens, changed_closes, selection=selection, strategy=cfg,
                                splitter=splitter, borrow=BORROW, entry_grid=(2.,))
    pd.testing.assert_frame_equal(result.selections[result.selections.fold == 0], changed.selections[changed.selections.fold == 0])
    pd.testing.assert_frame_equal(result.validation[result.validation.fold == 0], changed.validation[changed.validation.fold == 0])
    pd.testing.assert_frame_equal(result.nav[result.nav.date <= cutoff], changed.nav[changed.nav.date <= cutoff])
    np.testing.assert_allclose(result.nav.strategy_nav * cfg.initial_cash, result.nav.equity)
    np.testing.assert_allclose(result.nav.equity, cfg.initial_cash + result.nav.long_pnl + result.nav.short_pnl
                               - result.nav.cumulative_transaction_cost - result.nav.cumulative_borrow_cost)
    assert result.metadata["actual_tradability"] == "UNKNOWN"
    assert result.evaluation.metrics["maximum_drawdown"].status == "OK"


def test_unsettled_oos_leg_blocks_next_fold_without_erasing_liability():
    opens, closes = synthetic_panels(periods=600)
    cfg = PairTradingConfig(entry_z=5, diagnostic_interval=100, failure_pvalue=1.)
    splitter = ExpandingWalkForwardSplitter(initial_train_months=12)
    selection = SelectionConfig(train_end="2020-01-01")
    baseline = run_pair_research(opens, closes, selection=selection, strategy=cfg, splitter=splitter,
                                 borrow=BORROW, entry_grid=(5.,))
    final_day = baseline.folds.test_end.iloc[0]
    position = closes.index.get_loc(final_day)
    closes.iloc[position - 2, 0] += 15
    closes.iloc[position - 1, 0] += 15
    opens.iloc[position - 1, 0] += 15
    result = run_pair_research(opens, closes, selection=selection, strategy=cfg, splitter=splitter,
        borrow=BORROW, entry_grid=(5.,), fill_scenarios={final_day.strftime("%Y-%m-%d"): {"A": 0}})
    assert len(result.folds) == 1
    assert result.metadata["fold_continuation"] == "BLOCKED_UNRESOLVED_EXPOSURE"
    assert result.nav.short_liability.iloc[-1] > 0
    assert result.positions[result.positions.date == final_day].side.tolist() == ["short"]
    assert not result.failures.empty


def test_saved_config_replays_identical_research_and_tracker(tmp_path):
    config_path = Path(__file__).parents[2] / "configs/research/p5-b02-pairs-synthetic.json"
    config = json.loads(config_path.read_text())
    config["synthetic"]["periods"] = 470
    first = execute(config, tmp_path / "first")
    saved = json.loads((tmp_path / "first/config.json").read_text())
    replay = execute(saved, tmp_path / "replay")
    for key in ("nav", "selections", "folds", "trades", "positions", "failures", "signals", "validation"):
        pd.testing.assert_frame_equal(getattr(first, key), getattr(replay, key))
    tracker = ExperimentTracker(tmp_path / "replay/tracker")
    run = tracker.get_run("synthetic")["payload"]["run"]
    assert run["qualification"] == "SYNTHETIC"
    assert run["validation"]["actual_tradability"] == "UNKNOWN"
    assert tracker.reproduction("synthetic", verify_artifacts=True)["definition"]["parameters"] == saved


def test_noncointegrated_high_correlation_is_not_traded():
    opens, closes = synthetic_panels(seed=121, periods=800, relationship="noncointegrated")
    assert closes.corr().iloc[0, 1] > .99
    models, diagnostics = select_pairs(closes, SelectionConfig(train_end=closes.index[-1].isoformat()))
    assert not models
    assert "Engle-Granger" in diagnostics.iloc[0].reason
    result = run_pair_research(opens, closes, selection=SelectionConfig(train_end="2020-01-01"),
        strategy=PairTradingConfig(), splitter=ExpandingWalkForwardSplitter(initial_train_months=12),
        borrow=BORROW, entry_grid=(2.,))
    assert result.trades.empty
    assert result.nav.strategy_nav.eq(1).all()
    assert not result.selections.accepted.any()


def test_candidate_limit_and_missing_execution_prices():
    opens, closes, model, dates, cfg = setup_case()
    closes["C"] = closes.B * 2
    _, diagnostics = select_pairs(closes, SelectionConfig(train_end=model.train_end, max_candidates=1))
    assert len(diagnostics) == 1
    assert "price_correlation" in diagnostics
    closes = closes.drop(columns="C")
    opens.loc[dates[2], "A"] = np.nan
    with pytest.raises(ValueError, match="missing data"):
        simulate_pair(opens, closes, dates, model, cfg, BORROW)


def test_datahub_reader_preserves_missing_dates():
    from factorlab.data.hub.hub import DataHub
    from factorlab.data.hub.contracts import QueryResult, SourceMetadata

    class Adapter:
        capabilities = frozenset({"daily_bars"})

        def read(self, kind, **query):
            assert kind == "daily_bars"
            assert query["knowledge_as_of"] == "2020-01-05T00:00:00Z"
            index = pd.to_datetime(["2020-01-01", "2020-01-03"] if query["symbol"] == "000001.SZ" else ["2020-01-02", "2020-01-03"])
            return QueryResult(pd.DataFrame(dict(date=index, raw_open=[1., 2.], raw_close=[2., 3.])),
                               SourceMetadata(source_id="fixture", data_version="synthetic-v1"))
    hub = DataHub({"fixture": Adapter()})
    opens, closes, receipts = read_pair_panels(hub, "fixture", ["000001.SZ", "000002.SZ"], start="2020-01-01", end="2020-01-03",
                                              knowledge_as_of="2020-01-05T00:00:00Z")
    assert pd.isna(opens.loc["2020-01-02", "000001.SZ"])
    assert pd.isna(closes.loc["2020-01-01", "000002.SZ"])
    assert len(receipts) == 2
    assert receipts[0].metadata.evidence_status == ("PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED")
