"""Deterministic factor-evaluation and report artifact tests."""

import pandas as pd
import pytest

from factorlab.errors import SchemaError
from factorlab.metrics.factor import evaluate_factors
from factorlab.reports.generator import write_factor_report


def _evaluation_frame():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    rows = []
    for date_number, date in enumerate(dates):
        for number in range(10):
            factor = float(number if date_number == 0 else 9 - number)
            rows.append(
                {
                    "date": date,
                    "symbol": "%06d.SZ" % number,
                    "F1": factor,
                    "F2": -factor,
                    "EXCESS_RET5": factor,
                    "REGIME": "BULL" if date_number == 0 else "BEAR",
                }
            )
    return pd.DataFrame(rows)


def test_factor_evaluation_computes_ic_quantiles_autocorrelation_and_turnover():
    evaluation = evaluate_factors(
        _evaluation_frame(),
        ["F1", "F2"],
        quantiles=5,
        minimum_cross_section=5,
        group_column="REGIME",
    )
    all_daily = evaluation.daily_ic[evaluation.daily_ic["scope"] == "ALL"]
    assert all_daily.loc[all_daily["factor"] == "F1", "IC"].tolist() == pytest.approx([1.0, 1.0])
    assert all_daily.loc[all_daily["factor"] == "F1", "RankIC"].tolist() == pytest.approx([1.0, 1.0])
    assert all_daily.loc[all_daily["factor"] == "F2", "IC"].tolist() == pytest.approx([-1.0, -1.0])
    assert set(evaluation.summary["scope"]) == {"ALL", "BEAR", "BULL"}

    spread = evaluation.quantile_returns.query(
        "scope == 'ALL' and factor == 'F1' and quantile == 'Q5-Q1'"
    )
    assert spread["future_return"].tolist() == pytest.approx([8.0, 8.0])
    autocorrelation = evaluation.autocorrelation.query(
        "scope == 'ALL' and factor == 'F1'"
    )
    assert autocorrelation.iloc[0]["autocorrelation"] == pytest.approx(-1.0)
    turnover = evaluation.turnover.query("scope == 'ALL' and factor == 'F1'")
    assert turnover.iloc[0]["top_quantile_turnover"] == pytest.approx(1.0)
    assert evaluation.correlation.loc["F1", "F2"] == pytest.approx(-1.0)


def test_factor_evaluation_reports_coverage_and_invalid_small_cross_sections():
    frame = _evaluation_frame()
    frame.loc[frame.index[:5], "F1"] = None
    evaluation = evaluate_factors(frame, ["F1"], quantiles=5, minimum_cross_section=6)
    first = evaluation.daily_ic.sort_values("date").iloc[0]
    assert first["observations"] == 5
    assert first["coverage"] == pytest.approx(0.5)
    assert pd.isna(first["IC"])


def test_quantile_membership_does_not_depend_on_future_label_availability():
    frame = _evaluation_frame().loc[
        lambda value: value["date"] == pd.Timestamp("2024-01-02")
    ].copy()
    frame.loc[frame["F1"] == 9.0, "EXCESS_RET5"] = None

    evaluation = evaluate_factors(
        frame, ["F1"], quantiles=5, minimum_cross_section=5
    )
    top = evaluation.quantile_returns.query(
        "scope == 'ALL' and factor == 'F1' and quantile == 'Q5'"
    ).iloc[0]

    assert top["observations"] == 2
    assert top["labeled_observations"] == 1
    assert top["label_coverage"] == pytest.approx(0.5)
    assert top["future_return"] == pytest.approx(8.0)


def test_regime_temporal_metrics_never_jump_over_other_regime_dates():
    rows = []
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    regimes = ["BULL", "BEAR", "BULL"]
    for day, (date, regime) in enumerate(zip(dates, regimes)):
        for number in range(10):
            factor = float(number if day != 1 else 9 - number)
            rows.append(
                {
                    "date": date,
                    "symbol": "%06d.SZ" % number,
                    "F1": factor,
                    "EXCESS_RET5": factor,
                    "REGIME": regime,
                }
            )

    evaluation = evaluate_factors(
        pd.DataFrame(rows),
        ["F1"],
        quantiles=5,
        minimum_cross_section=5,
        group_column="REGIME",
    )
    final_date = pd.Timestamp("2024-01-04")
    bull_autocorrelation = evaluation.autocorrelation.loc[
        evaluation.autocorrelation["scope"].eq("BULL")
        & evaluation.autocorrelation["date"].eq(final_date)
    ].iloc[0]
    bull_turnover = evaluation.turnover.loc[
        evaluation.turnover["scope"].eq("BULL")
        & evaluation.turnover["date"].eq(final_date)
    ].iloc[0]

    assert bull_autocorrelation["autocorrelation"] == pytest.approx(-1.0)
    assert bull_turnover["top_quantile_turnover"] == pytest.approx(1.0)


def test_regime_group_must_be_constant_within_each_date():
    frame = _evaluation_frame()
    frame.loc[frame.index[0], "REGIME"] = "BEAR"

    with pytest.raises(SchemaError, match="at most one value per date"):
        evaluate_factors(
            frame,
            ["F1"],
            quantiles=5,
            minimum_cross_section=5,
            group_column="REGIME",
        )


@pytest.mark.parametrize("missing_session", ["absent", "invalid_factors"])
def test_temporal_metrics_do_not_jump_over_unavailable_market_session(missing_session):
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    frame = _evaluation_frame().drop(columns=["REGIME"])
    final = frame.loc[frame["date"].eq(dates[0])].copy()
    final["date"] = dates[2]
    if missing_session == "absent":
        frame = frame.loc[frame["date"].eq(dates[0])]
    else:
        frame.loc[frame["date"].eq(dates[1]), "F1"] = None
    frame = pd.concat([frame, final], ignore_index=True)

    evaluation = evaluate_factors(
        frame,
        ["F1"],
        quantiles=5,
        minimum_cross_section=5,
        calendar=dates,
    )

    last_autocorrelation = evaluation.autocorrelation.loc[
        evaluation.autocorrelation["date"].eq(dates[2])
    ].iloc[0]
    assert last_autocorrelation["observations"] == 0
    assert pd.isna(last_autocorrelation["autocorrelation"])
    assert evaluation.turnover.empty


def test_grouped_single_date_has_no_temporal_observations():
    frame = _evaluation_frame().loc[
        lambda values: values["date"].eq(pd.Timestamp("2024-01-02"))
    ]
    evaluation = evaluate_factors(
        frame,
        ["F1"],
        quantiles=5,
        minimum_cross_section=5,
        group_column="REGIME",
    )
    assert evaluation.autocorrelation.empty
    assert evaluation.turnover.empty


def test_report_writes_required_csv_markdown_and_png_artifacts(tmp_path):
    evaluation = evaluate_factors(
        _evaluation_frame(), ["F1", "F2"], quantiles=5, minimum_cross_section=5
    )
    summary = write_factor_report(evaluation, tmp_path)
    expected = {
        "factor_report.csv",
        "daily_ic.csv",
        "quantile_returns.csv",
        "factor_autocorrelation.csv",
        "factor_turnover.csv",
        "factor_correlation.csv",
        "factor_ic.png",
        "rank_ic.png",
        "factor_correlation.png",
        "quantile_returns.png",
        "summary.md",
    }
    assert expected.issubset({path.name for path in tmp_path.iterdir()})
    assert summary.read_text(encoding="utf-8").startswith("# FactorLab Phase 1")
    assert all((tmp_path / filename).stat().st_size > 0 for filename in expected)
