"""Historical CSI500 diagnostics using the frozen production state function.

UNKNOWN is a diagnostic overlay only; MARKET_REGIME retains production values.
All daily prices, states, moving averages and returns use observations <= t.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from factorlab.errors import SchemaError
from factorlab.market.regime import MARKET_REGIMES, build_market_regime_features


DIAGNOSTIC_REGIMES = MARKET_REGIMES + ("UNKNOWN",)
DISCLAIMER = "Historical descriptive statistics; NOT predictive returns. NO LIVE MONEY."


@dataclass(frozen=True)
class MarketRegimeDiagnosis:
    daily: pd.DataFrame
    summary: pd.DataFrame
    spells: pd.DataFrame
    annual: pd.DataFrame
    transitions: pd.DataFrame
    coverage: Dict[str, Any]


def _boundary(value: Any) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError("Invalid diagnostic date boundary") from exc
    if pd.isna(result) or result.tzinfo is not None or result != result.normalize():
        raise SchemaError("Diagnostic boundary must be a timezone-naive date at midnight")
    return result


def _spells(daily: pd.DataFrame) -> pd.DataFrame:
    states = daily["DIAGNOSTIC_REGIME"]
    groups = states.ne(states.shift()).cumsum()
    rows = []
    for number, (_, group) in enumerate(daily.groupby(groups, sort=False)):
        first, last = int(group.index[0]), int(group.index[-1])
        rows.append({
            "spell_id": number + 1,
            "regime": group.iloc[0]["DIAGNOSTIC_REGIME"],
            "start": group.iloc[0]["date"], "end": group.iloc[-1]["date"],
            "sessions": len(group),
            "calendar_days": (group.iloc[-1]["date"] - group.iloc[0]["date"]).days + 1,
            "left_censored": first == 0 or daily.iloc[first - 1]["DIAGNOSTIC_REGIME"] == "UNKNOWN",
            "right_censored": last == len(daily) - 1 or daily.iloc[last + 1]["DIAGNOSTIC_REGIME"] == "UNKNOWN",
            "plot_end": daily.iloc[last + 1]["date"] if last + 1 < len(daily)
                        else group.iloc[-1]["date"] + pd.Timedelta(days=1),
        })
    return pd.DataFrame(rows)


def _return_statistics(group: pd.DataFrame) -> Dict[str, Any]:
    returns = group["daily_return"].dropna()
    volatility = float(returns.std(ddof=1)) if len(returns) >= 2 else np.nan
    return {
        "return_samples": len(returns),
        "return_coverage": len(returns) / len(group) if len(group) else np.nan,
        "mean_daily_return": float(returns.mean()) if len(returns) else np.nan,
        "daily_volatility": volatility,
        "annualized_volatility_252": volatility * np.sqrt(252.0),
    }


def diagnose_market_regime(
    benchmark: pd.DataFrame,
    start: Any,
    end: Any,
    calendar: Optional[pd.DatetimeIndex] = None,
) -> MarketRegimeDiagnosis:
    """Compute on all available prehistory, then restrict report to [start,end].

    Input must be strictly date-sorted, as required by the frozen function.
    An optional independently supplied actual session calendar exposes absent
    rows as UNKNOWN. Default coverage is only relative to observed cache dates;
    business-day guesses are never used to invent real trading sessions.
    """
    start_date, end_date = _boundary(start), _boundary(end)
    if start_date > end_date:
        raise SchemaError("Diagnostic start must be on or before end")
    # Delegate price/symbol/date validation and ALL state decisions to production.
    validated = build_market_regime_features(benchmark, price_column="adj_close")
    frame = benchmark.copy().reset_index(drop=True)
    frame["date"] = validated["date"].to_numpy()
    frame = frame.loc[frame["date"] <= end_date].reset_index(drop=True)
    frame["source_row_present"] = True
    if calendar is not None:
        dates = pd.DatetimeIndex(calendar)
        if (dates.hasnans or dates.has_duplicates or dates.tz is not None
                or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())):
            raise SchemaError("Diagnostic calendar must contain unique sorted naive dates at midnight")
        if not frame["date"].isin(dates).all():
            raise SchemaError("Benchmark contains dates outside the supplied calendar")
        dates = dates[dates <= end_date]
        frame = frame.set_index("date").reindex(dates).rename_axis("date").reset_index()
        frame["source_row_present"] = frame["source_row_present"].eq(True)
        # Reindex does not fill prices; identity for absent observations is known.
        frame["symbol"] = "000905.SH"
    if frame.empty or start_date not in set(frame["date"]) or end_date not in set(frame["date"]):
        raise SchemaError("Benchmark does not cover both requested diagnostic boundaries")
    if frame["date"].dt.dayofweek.ge(5).any():
        raise SchemaError("A-share diagnostic calendar contains a weekend observation")

    daily = build_market_regime_features(frame, price_column="adj_close")
    prices = frame["adj_close"].reset_index(drop=True)
    daily["adj_close"] = prices
    daily["MA20"] = prices.rolling(20, min_periods=20).mean()
    daily["MA60"] = prices.rolling(60, min_periods=60).mean()
    daily["valid_prices_in_last_60"] = prices.notna().rolling(60, min_periods=1).sum().astype(int)
    reason = np.select(
        [~frame["source_row_present"].to_numpy(), prices.isna().to_numpy(),
         np.arange(len(frame)) < 59, daily["MA60"].isna().to_numpy()],
        ["MISSING_SESSION", "MISSING_PRICE", "INSUFFICIENT_HISTORY", "INCOMPLETE_MA_WINDOW"],
        default="",
    )
    daily["unknown_reason"] = reason
    daily["DIAGNOSTIC_REGIME"] = daily["MARKET_REGIME"].where(reason == "", "UNKNOWN")
    daily["source_row_present"] = frame["source_row_present"].to_numpy()
    daily["daily_return"] = prices.pct_change(fill_method=None)
    if np.isinf(daily["daily_return"].to_numpy()).any():
        raise SchemaError("Diagnostic daily return overflow")
    warmup_sessions = int(daily["date"].lt(start_date).sum())
    daily = daily.loc[daily["date"].between(start_date, end_date)].reset_index(drop=True)
    spells = _spells(daily)
    summary_rows, annual_rows = [], []
    for state in DIAGNOSTIC_REGIMES:
        group = daily.loc[daily["DIAGNOSTIC_REGIME"].eq(state)]
        durations = spells.loc[spells["regime"].eq(state)]
        summary_rows.append({
            "regime": state, "sessions": len(group), "share": len(group) / len(daily),
            "spells": len(durations),
            "mean_spell_sessions": durations["sessions"].mean(),
            "median_spell_sessions": durations["sessions"].median(),
            "max_spell_sessions": durations["sessions"].max(),
            "censored_spells": int((durations["left_censored"] | durations["right_censored"]).sum()),
            **_return_statistics(group),
        })
        for year, yearly in daily.groupby(daily["date"].dt.year, sort=True):
            year_group = yearly.loc[yearly["DIAGNOSTIC_REGIME"].eq(state)]
            annual_rows.append({"year": int(year), "regime": state,
                                "sessions": len(year_group), "share": len(year_group) / len(yearly),
                                **_return_statistics(year_group)})
    transitions = []
    previous, current = daily["DIAGNOSTIC_REGIME"].shift(), daily["DIAGNOSTIC_REGIME"]
    for left in DIAGNOSTIC_REGIMES:
        for right in DIAGNOSTIC_REGIMES:
            if left != right:
                transitions.append({"from_regime": left, "to_regime": right,
                                    "count": int((previous.eq(left) & current.eq(right)).sum()),
                                    "kind": "unknown_boundary" if "UNKNOWN" in (left, right) else "state_switch"})
    transitions = pd.DataFrame(transitions)
    coverage = {
        "requested_start": start_date.date().isoformat(), "requested_end": end_date.date().isoformat(),
        "calendar_basis": "independent_supplied_sessions" if calendar is not None else "observed_benchmark_cache_dates",
        "independent_calendar_verified": calendar is not None,
        "sessions": len(daily), "warmup_sessions": warmup_sessions,
        "source_rows": int(daily["source_row_present"].sum()),
        "source_row_coverage": float(daily["source_row_present"].mean()),
        "valid_prices": int(daily["adj_close"].notna().sum()),
        "price_coverage": float(daily["adj_close"].notna().mean()),
        "known_sessions": int(daily["DIAGNOSTIC_REGIME"].ne("UNKNOWN").sum()),
        "state_coverage": float(daily["DIAGNOSTIC_REGIME"].ne("UNKNOWN").mean()),
        "return_samples": int(daily["daily_return"].notna().sum()),
        "return_coverage": float(daily["daily_return"].notna().mean()),
        "state_switches": int(transitions.loc[transitions["kind"].eq("state_switch"), "count"].sum()),
        "unknown_boundaries": int(transitions.loc[transitions["kind"].eq("unknown_boundary"), "count"].sum()),
        "unknown_reasons": {str(k): int(v) for k, v in daily.loc[daily["DIAGNOSTIC_REGIME"].eq("UNKNOWN"), "unknown_reason"].value_counts().items()},
    }
    return MarketRegimeDiagnosis(daily, pd.DataFrame(summary_rows), spells,
                                pd.DataFrame(annual_rows).sort_values(["year", "regime"]).reset_index(drop=True),
                                transitions, coverage)
