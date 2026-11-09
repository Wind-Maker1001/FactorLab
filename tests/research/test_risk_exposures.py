"""Historical optional exposure diagnostics and point-in-time boundaries."""

import numpy as np
import pandas as pd
import pytest

from factorlab.metrics.exposures import diagnose_exposures


EVIDENCE_COLUMNS = [
    "symbol", "effective_date", "known_at", "source", "kind", "value", "unit", "source_kind",
]


def positions(dates=("2024-01-02",)):
    return pd.DataFrame([(date, symbol, weight) for date in dates
                         for symbol, weight in (("A", .6), ("B", .2))],
                        columns=["date", "symbol", "weight"])


def observation(symbol, kind, value, effective="2024-01-01", known="2024-01-01"):
    unit = {"industry": "classification", "market_cap": "CNY", "momentum": "return",
            "volatility": "annualized_return"}[kind]
    return [symbol, effective, known, "user_history", kind, value, unit, "SYNTHETIC"]


def evidence():
    return pd.DataFrame([observation(symbol, kind, value)
                         for symbol, values in (("A", ("tech", 100, .1, .3)),
                                                ("B", ("bank", 200, -.2, .5)))
                         for kind, value in zip(("industry", "market_cap", "momentum", "volatility"), values)],
                        columns=EVIDENCE_COLUMNS)


def at(result, metric, date="2024-01-02"):
    return result.loc[result.date.eq(pd.Timestamp(date)) & result.metric.eq(metric)].iloc[0]


def test_nav_weighted_concentration_industries_and_explicit_style_values():
    result = diagnose_exposures(positions(), evidence())
    assert list(result) == ["date", "metric", "value", "status", "reason"]
    for metric, expected in {"security_hhi": .4, "max_security_weight": .6,
                             "industry_hhi": .4, "industry_weight:tech": .6,
                             "industry_weight:bank": .2, "market_cap": 100,
                             "momentum": .02, "volatility": .28}.items():
        row = at(result, metric)
        assert row.value == pytest.approx(expected)
        assert row.status == "OK"
        assert row.reason == ""


def test_classification_changes_are_effective_and_known_before_close():
    history = evidence()
    changes = pd.DataFrame([
        observation("B", "industry", "tech", "2024-01-03", "2024-01-03 15:00"),
    ], columns=EVIDENCE_COLUMNS)
    result = diagnose_exposures(positions(("2024-01-02", "2024-01-03", "2024-01-04")),
                                pd.concat([history, changes], ignore_index=True))
    assert at(result, "industry_hhi").value == pytest.approx(.4)
    assert at(result, "industry_hhi", "2024-01-03").value == pytest.approx(.4)
    assert at(result, "industry_hhi", "2024-01-04").value == pytest.approx(.64)
    assert at(result, "industry_weight:tech", "2024-01-04").value == pytest.approx(.8)


def test_future_effective_and_future_known_perturbations_do_not_change_past():
    baseline = diagnose_exposures(positions(), evidence())
    future = pd.DataFrame([
        observation("A", "industry", "bank", "2024-02-01", "2024-01-01"),
        observation("A", "market_cap", 999999, "2024-01-02", "2024-01-03"),
        observation("B", "momentum", 500, "2024-01-02", "2024-01-02 07:00:00+00:00"),
    ], columns=EVIDENCE_COLUMNS)
    pd.testing.assert_frame_equal(baseline, diagnose_exposures(
        positions(), pd.concat([evidence(), future], ignore_index=True)))


@pytest.mark.parametrize("history", [None, pd.DataFrame(columns=EVIDENCE_COLUMNS)])
def test_absent_optional_history_preserves_concentration(history):
    result = diagnose_exposures(positions(), history)
    assert at(result, "security_hhi").status == "OK"
    for metric in ("industry_hhi", "market_cap", "momentum", "volatility"):
        row = at(result, metric)
        assert row.status == "NOT_COMPUTABLE"
        assert pd.isna(row.value)
        assert row.reason


def test_missing_coverage_is_not_renormalized_or_future_filled():
    history = evidence()
    history = history.loc[~(history.symbol.eq("B") & history.kind.eq("market_cap"))]
    future = pd.DataFrame([observation("B", "market_cap", 200, "2024-01-03", "2024-01-03")],
                          columns=EVIDENCE_COLUMNS)
    result = diagnose_exposures(positions(), pd.concat([history, future], ignore_index=True))
    assert at(result, "market_cap").status == "NOT_COMPUTABLE"
    assert at(result, "momentum").status == "OK"


@pytest.mark.parametrize("column,value", [("value", np.nan), ("value", np.inf),
                                           ("value", -1), ("unit", "USD"),
                                           ("source", ""), ("source_kind", "")])
def test_invalid_selected_history_is_not_reported_as_complete(column, value):
    history = evidence()
    history.loc[history.symbol.eq("A") & history.kind.eq("market_cap"), column] = value
    assert at(diagnose_exposures(positions(), history), "market_cap").status == "NOT_COMPUTABLE"


def test_invalid_latest_observation_does_not_fall_back_to_old_value():
    update = pd.DataFrame([observation("A", "market_cap", "invalid", "2024-01-02", "2024-01-02")],
                          columns=EVIDENCE_COLUMNS)
    result = diagnose_exposures(positions(), pd.concat([evidence(), update], ignore_index=True))
    assert at(result, "market_cap").status == "NOT_COMPUTABLE"


def test_ambiguous_winning_sources_are_not_arbitrarily_selected():
    history = evidence()
    duplicate = history.iloc[[0]].copy()
    duplicate["source"] = "other"
    result = diagnose_exposures(positions(), pd.concat([history, duplicate], ignore_index=True))
    assert at(result, "industry_hhi").status == "NOT_COMPUTABLE"


def test_zero_weight_missing_symbol_does_not_require_evidence():
    portfolio = positions()
    portfolio.loc[len(portfolio)] = ["2024-01-02", "C", 0]
    assert at(diagnose_exposures(portfolio, evidence()), "market_cap").value == pytest.approx(100)


def test_short_weights_remain_signed_and_concentration_uses_absolute_max():
    portfolio = positions()
    portfolio["weight"] = [-.6, .2]
    result = diagnose_exposures(portfolio, evidence())
    assert at(result, "security_hhi").value == pytest.approx(.4)
    assert at(result, "max_security_weight").value == pytest.approx(.6)
    assert at(result, "market_cap").value == pytest.approx(-20)
    assert at(result, "industry_weight:tech").value == pytest.approx(-.6)


def test_malformed_schema_rejected_but_empty_positions_have_tidy_schema():
    with pytest.raises(ValueError, match="evidence requires"):
        diagnose_exposures(positions(), pd.DataFrame({"symbol": ["A"]}))
    with pytest.raises(ValueError, match="positions require"):
        diagnose_exposures(pd.DataFrame({"symbol": ["A"]}))
    with pytest.raises(ValueError, match="unique"):
        diagnose_exposures(pd.concat([positions(), positions()]))
    result = diagnose_exposures(positions().iloc[:0])
    assert result.empty
    assert list(result) == ["date", "metric", "value", "status", "reason"]


def test_cash_only_sessions_have_zero_concentration_without_fabricated_styles():
    result = diagnose_exposures(positions().iloc[:0], calendar=pd.to_datetime(['2024-01-02']))
    assert at(result, 'security_hhi').value == 0
    assert at(result, 'max_security_weight').value == 0
    assert at(result, 'momentum').status == 'NOT_COMPUTABLE'
