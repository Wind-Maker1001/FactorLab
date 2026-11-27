"""Phase 6: Multi-strategy shared account joint risk research built on #260 public CapitalPolicy.

Strictly separates Alpaca (US Equities USD account) and Binance (USD-M Perpetual USDT account).
Never assumes cash or margin can be shared across exchanges, never substitutes a naive sum
of independent account returns for true shared-cash / shared-inventory / risk-budget execution,
and never models daily cross-exchange rebalancing or discards weekend/holiday Binance PnL.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Iterable

import numpy as np
import pandas as pd

from factorlab.research.monte_carlo import _loss_tail
from factorlab.trading.capital import CapitalPolicy, CapitalPolicyMode
from factorlab.trading.portfolio_execution import PortfolioStore

from .config import DEFAULT_TICKERS, ExternalValidationConfig
from .replay import (
    IndependentReplayResult,
    MarketPanel,
    build_signal_provider,
    compute_risk_metrics,
)

# Explicit cross-market calendar, valuation clock, and FX conversion constants
CROSS_MARKET_CALENDAR = "DAILY_NATURAL_UTC_CALENDAR_235959_UTC"
CROSS_MARKET_VALUATION_SPEC = {
    "calendar": CROSS_MARKET_CALENDAR,
    "valuation_cutoff_utc": "23:59:59+00:00",
    "alpaca_session_timezone": "America/New_York",
    "alpaca_close_convention": "NYSE official close (16:00 ET / 13:00 ET early close, always <= 21:00 UTC on same date; strictly causal before 23:59:59 UTC)",
    "binance_bar_timezone": "UTC",
    "binance_close_convention": "Last 1h UTC bar closing at or before 23:59:59 UTC (23:00 UTC open_time bar closes at 24:00 UTC)",
    "alpaca_closed_day_policy": "Carry forward last valid NYSE close NAV ONLY on confirmed NYSE closed dates (weekends/official NYSE holidays); raise ValueError on any missing expected NYSE trading session.",
    "fx_conversion_assumption": "FIXED_PARITY_1_USD_EQ_1_USDT_NO_DEPEG_MODELING",
    "usd_per_usdt": 1.0,
    "rebalancing_assumed": False,
    "cross_exchange_transfers_assumed": False,
    "shared_margin_assumed": False,
}

# Known one-off NYSE unscheduled full-day closures (national days of mourning / weather)
_NYSE_SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2001, 9, 11),
        date(2001, 9, 12),
        date(2001, 9, 13),
        date(2001, 9, 14),
        date(2004, 6, 11),  # Ronald Reagan
        date(2007, 1, 2),   # Gerald Ford
        date(2012, 10, 29), # Hurricane Sandy
        date(2012, 10, 30), # Hurricane Sandy
        date(2018, 12, 5),  # George H.W. Bush
        date(2025, 1, 9),   # Jimmy Carter
    }
)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Butcher's algorithm) for Western Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_fixed_holiday(year: int, month: int, day: int) -> date | None:
    """NYSE observation rule for fixed-date holidays (Sat -> Fri, Sun -> Mon)."""
    dt = date(year, month, day)
    if dt.weekday() == 5:
        return dt - timedelta(days=1)
    if dt.weekday() == 6:
        return dt + timedelta(days=1)
    return dt


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (1 if month == 12 else 0), (1 if month == 12 else month + 1), 1)
    last = nxt - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _fallback_nyse_holidays(year: int) -> set[date]:
    """Deterministic NYSE holiday rules used when exchange_calendars is unavailable or out of range."""
    hols: set[date] = set()
    # New Year's Day (note: if Jan 1 is Saturday, NYSE observes it only if not previous year? Actually NYSE does NOT close Dec 31!)
    nyd = date(year, 1, 1)
    if nyd.weekday() == 6:
        hols.add(date(year, 1, 2))
    elif nyd.weekday() < 5:
        hols.add(nyd)
    # MLK Jr. Day: 3rd Monday in January
    hols.add(_nth_weekday(year, 1, 0, 3))
    # Presidents' Day: 3rd Monday in February
    hols.add(_nth_weekday(year, 2, 0, 3))
    # Good Friday: 2 days before Easter Sunday
    hols.add(_easter_sunday(year) - timedelta(days=2))
    # Memorial Day: last Monday in May
    hols.add(_last_weekday(year, 5, 0))
    # Juneteenth (NYSE holiday starting 2022)
    if year >= 2022:
        obs = _observed_fixed_holiday(year, 6, 19)
        if obs is not None:
            hols.add(obs)
    # Independence Day
    obs_jul4 = _observed_fixed_holiday(year, 7, 4)
    if obs_jul4 is not None:
        hols.add(obs_jul4)
    # Labor Day: 1st Monday in September
    hols.add(_nth_weekday(year, 9, 0, 1))
    # Thanksgiving Day: 4th Thursday in November
    hols.add(_nth_weekday(year, 11, 3, 4))
    # Christmas Day
    obs_xmas = _observed_fixed_holiday(year, 12, 25)
    if obs_xmas is not None:
        hols.add(obs_xmas)
    for sp in _NYSE_SPECIAL_CLOSURES:
        if sp.year == year:
            hols.add(sp)
    return hols


def get_confirmed_nyse_closed_dates(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    *,
    extra_closed_dates: Iterable[pd.Timestamp | str | date] = (),
) -> tuple[set[pd.Timestamp], str]:
    """Return confirmed NYSE closed calendar dates in [start_date, end_date] and the calendar authority used."""
    s = pd.Timestamp(start_date).normalize()
    e = pd.Timestamp(end_date).normalize()
    if s.tzinfo is not None:
        s = s.tz_localize(None)
    if e.tzinfo is not None:
        e = e.tz_localize(None)
    all_days = pd.date_range(s, e, freq="D")
    extra = {pd.Timestamp(d).tz_localize(None).normalize() if pd.Timestamp(d).tzinfo is not None else pd.Timestamp(d).normalize() for d in extra_closed_dates}

    try:
        import exchange_calendars as xcals  # type: ignore[import-untyped]

        xnys = xcals.get_calendar("XNYS")
        first_s = pd.Timestamp(xnys.first_session).tz_localize(None).normalize()
        last_s = pd.Timestamp(xnys.last_session).tz_localize(None).normalize()
        if s >= first_s and e <= last_s:
            open_sessions = {pd.Timestamp(d).tz_localize(None).normalize() for d in xnys.sessions_in_range(s, e)}
            closed = {d for d in all_days if d not in open_sessions} | extra
            return closed, "exchange_calendars:XNYS"
    except Exception:
        pass

    hols_by_year: set[date] = set()
    for yr in range(s.year, e.year + 1):
        hols_by_year.update(_fallback_nyse_holidays(yr))
    closed = {
        d
        for d in all_days
        if d.weekday() >= 5 or d.date() in hols_by_year
    } | extra
    return closed, "deterministic_nyse_rule_fallback"


def _normalize_alpaca_session_nav(alpaca_nav: pd.Series) -> pd.Series:
    """Normalize Alpaca EOD NAV index to naive NYSE trading dates (converting tz-aware stamps via America/New_York)."""
    if len(alpaca_nav) == 0:
        raise ValueError("Alpaca NAV series is empty.")
    s = alpaca_nav.copy()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").normalize().tz_localize(None)
    else:
        idx = idx.normalize()
    s.index = idx
    if s.index.duplicated().any():
        dups = [str(d.date()) for d in s.index[s.index.duplicated()].unique()]
        raise ValueError(f"Duplicate dates in Alpaca NAV series: {dups}")
    s = s.sort_index().astype(np.float64)
    if not np.isfinite(s.to_numpy()).all() or (s <= 0.0).any():
        raise ValueError("Alpaca NAV contains non-finite or non-positive values.")
    return s


def _normalize_binance_daily_nav(binance_nav: pd.Series) -> pd.Series:
    """Normalize Binance daily NAV index to naive UTC calendar dates (converting tz-aware stamps via UTC)."""
    if len(binance_nav) == 0:
        raise ValueError("Binance NAV series is empty.")
    s = binance_nav.copy()
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").normalize().tz_localize(None)
    else:
        idx = idx.normalize()
    s.index = idx
    if s.index.duplicated().any():
        dups = [str(d.date()) for d in s.index[s.index.duplicated()].unique()]
        raise ValueError(f"Duplicate dates in Binance NAV series: {dups}")
    s = s.sort_index().astype(np.float64)
    if not np.isfinite(s.to_numpy()).all() or (s <= 0.0).any():
        raise ValueError("Binance NAV contains non-finite or non-positive values.")
    return s


def align_cross_market_navs(
    alpaca_nav: pd.Series,
    binance_nav: pd.Series,
    *,
    binance_initial_nav: float | None = None,
    extra_alpaca_closed_dates: Iterable[pd.Timestamp | str | date] = (),
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Align raw Alpaca USD NAV and Binance USDT NAV on a unified natural UTC calendar BEFORE computing returns.

    Rules (P2-01):
    1. Timezones: Alpaca timestamps map to NYSE session dates (America/New_York); NYSE close (16:00 ET / 20:00-21:00 UTC)
       occurs strictly before the 23:59:59 UTC daily valuation cutoff (zero lookahead). Binance timestamps map to UTC dates.
    2. Window start: If ``binance_initial_nav`` is given (e.g. initial capital immediately before bar 0 on day t_0),
       prepend a pre-window anchor at ``t_0 - 1d`` whenever Alpaca also has a valid anchor NAV on or before ``t_0 - 1d``,
       preserving 100% of Binance day-0 PnL. Otherwise start at the first calendar date where both accounts have a valid
       anchor NAV (Alpaca may carry a prior session onto a confirmed closed start date, never back-filling into the future).
    3. Window end: End at ``min(binance_end, alpaca_valid_end)``, where ``alpaca_valid_end`` extends beyond the last
       observed Alpaca session ONLY across immediately following confirmed NYSE closed days (weekends/holidays) and
       stops strictly before the next expected NYSE open session.
    4. Confirmed closure vs genuine missing data: Within ``[start, end]``, Alpaca NAV is carried forward ONLY on confirmed
       NYSE closed dates (weekends / official NYSE holidays). Missing Alpaca NAV on ANY expected NYSE trading session
       raises ValueError (never silently filled as closed or zero return). Missing Binance NAV on ANY natural calendar day
       (Binance trades 24/7) also raises ValueError.
    """
    alp = _normalize_alpaca_session_nav(alpaca_nav)
    bn = _normalize_binance_daily_nav(binance_nav)

    # Optionally prepend explicit pre-window anchor for Binance initial capital before bar 0
    used_pre_window_anchor = False
    bn_first_trading_day = bn.index.min()
    if binance_initial_nav is not None:
        if not np.isfinite(binance_initial_nav) or binance_initial_nav <= 0.0:
            raise ValueError("binance_initial_nav must be finite and positive.")
        anchor_day = bn_first_trading_day - pd.Timedelta(days=1)
        if alp.index.min() <= anchor_day:
            bn = pd.concat([pd.Series([float(binance_initial_nav)], index=pd.DatetimeIndex([anchor_day])), bn]).sort_index()
            used_pre_window_anchor = True

    cal_min = min(alp.index.min(), bn.index.min())
    cal_max = max(alp.index.max(), bn.index.max())
    closed_dates, calendar_source = get_confirmed_nyse_closed_dates(
        cal_min, cal_max, extra_closed_dates=extra_alpaca_closed_dates
    )

    # Determine Alpaca's valid trailing coverage end: last observed Alpaca date plus consecutive confirmed closed days
    alp_last_obs = alp.index.max()
    alp_valid_end = alp_last_obs
    probe = alp_last_obs + pd.Timedelta(days=1)
    while probe <= cal_max and probe in closed_dates:
        alp_valid_end = probe
        probe += pd.Timedelta(days=1)

    # Determine aligned window [start_date, end_date]:
    # Alpaca has valid anchor at d iff d >= alp.index.min() (so prior session exists if d is closed).
    start_date = max(alp.index.min(), bn.index.min())
    end_date = min(alp_valid_end, bn.index.max())

    if start_date > end_date:
        raise ValueError(
            f"No overlapping natural-calendar coverage between Alpaca [{alp.index.min().date()}..{alp_valid_end.date()}] "
            f"and Binance [{bn.index.min().date()}..{bn.index.max().date()}]."
        )

    calendar = pd.date_range(start_date, end_date, freq="D")
    if len(calendar) < 2:
        raise ValueError(f"Aligned natural calendar has fewer than 2 valuation dates ({len(calendar)}).")

    # Verify Binance has complete 24/7 natural-calendar coverage inside [start_date, end_date]
    missing_bn = [d for d in calendar if d not in bn.index]
    if missing_bn:
        raise ValueError(
            f"Missing Binance NAV on natural calendar day(s): {[str(d.date()) for d in missing_bn[:10]]}"
        )

    # Build Alpaca natural-calendar NAV with strict separation between confirmed closed days and missing open sessions
    alp_aligned = np.empty(len(calendar), dtype=np.float64)
    alp_is_closed = np.zeros(len(calendar), dtype=bool)
    alp_missing_open_sessions: list[str] = []

    # Seed last_valid_alp from the most recent Alpaca observation on or before start_date
    prior_alp = alp.loc[:start_date]
    if len(prior_alp) == 0:
        raise ValueError(f"Alpaca has no valid NAV observation on or before window start {start_date.date()}.")
    last_valid_alp = float(prior_alp.iloc[-1])

    for i, d in enumerate(calendar):
        in_alp = d in alp.index
        is_closed = d in closed_dates
        if in_alp:
            last_valid_alp = float(alp.at[d])
            alp_aligned[i] = last_valid_alp
            alp_is_closed[i] = False
        elif is_closed:
            alp_aligned[i] = last_valid_alp
            alp_is_closed[i] = True
        else:
            alp_missing_open_sessions.append(str(d.date()))

    if alp_missing_open_sessions:
        raise ValueError(
            "Missing Alpaca NAV on expected NYSE trading session(s) (refusing to forward-fill missing data as closed/zero return): "
            f"{alp_missing_open_sessions}"
        )

    bn_aligned = bn.reindex(calendar).to_numpy(dtype=np.float64)

    aligned = pd.DataFrame(
        {
            "alpaca_nav": alp_aligned,
            "binance_nav": bn_aligned,
            "alpaca_market_closed": alp_is_closed,
        },
        index=calendar,
    )
    aligned["alpaca_daily_ret"] = aligned["alpaca_nav"].pct_change()
    aligned["binance_daily_ret"] = aligned["binance_nav"].pct_change()

    # Count weekend vs US holiday closed days across the return evaluation window (excluding t_0 anchor row)
    eval_slice = aligned.iloc[1:]
    closed_eval = eval_slice.index[eval_slice["alpaca_market_closed"]]
    weekend_days = int(sum(1 for d in closed_eval if d.weekday() >= 5))
    us_holiday_weekdays = [str(d.date()) for d in closed_eval if d.weekday() < 5]

    # Verify consecutive-closure runs (e.g. 3-day holiday weekends)
    max_consec_closed = 0
    cur_run = 0
    for flag in eval_slice["alpaca_market_closed"].to_numpy():
        if flag:
            cur_run += 1
            max_consec_closed = max(max_consec_closed, cur_run)
        else:
            cur_run = 0

    # Verify zero omitted Binance PnL relative to the raw Binance NAV series over [start_date, end_date]
    raw_bn_total_ret = float(bn.at[end_date] / bn.at[start_date] - 1.0)
    aligned_bn_compounded_ret = float(np.prod(1.0 + eval_slice["binance_daily_ret"].to_numpy(dtype=np.float64)) - 1.0)
    bn_pnl_reconciliation_err = abs(raw_bn_total_ret - aligned_bn_compounded_ret)

    meta = {
        **CROSS_MARKET_VALUATION_SPEC,
        "nyse_calendar_authority": calendar_source,
        "used_pre_window_anchor": used_pre_window_anchor,
        "anchor_date": str(calendar[0].date()),
        "return_start_date": str(calendar[1].date()),
        "return_end_date": str(calendar[-1].date()),
        "valuation_dates_count": int(len(calendar)),
        "natural_calendar_return_days": int(len(eval_slice)),
        "alpaca_open_session_days": int((~eval_slice["alpaca_market_closed"]).sum()),
        "alpaca_confirmed_closed_days": int(eval_slice["alpaca_market_closed"].sum()),
        "alpaca_weekend_closed_days": weekend_days,
        "alpaca_us_holiday_weekdays": us_holiday_weekdays,
        "alpaca_max_consecutive_closed_days": int(max_consec_closed),
        "alpaca_unexpected_missing_sessions": 0,
        "binance_omitted_days": 0,
        "binance_pnl_reconciliation_error": float(bn_pnl_reconciliation_err),
    }
    return aligned, meta


def aggregate_isolated_cross_exchange_portfolios(
    aligned_navs: pd.DataFrame,
    *,
    binance_weights: Iterable[float] = (0.0, 0.05, 0.10, 0.20),
    total_initial_capital_usd: float = 300_000.0,
    usd_per_usdt: float = 1.0,
) -> list[dict[str, Any]]:
    """Aggregate two strictly isolated accounts by compounding each account's own NAV independently (P2-02).

    Never applies fixed initial weights to daily returns (which would imply daily cross-exchange rebalancing).
    Each account starts with its initial dollar-equivalent allocation, compounds solely from its own NAV,
    and combined portfolio equity is the sum of dollar-equivalent equities on the unified natural calendar.
    All portfolio returns, drawdowns, VaR, CVaR, and realized weight drift are derived from total equity.
    """
    if total_initial_capital_usd <= 0.0 or not np.isfinite(total_initial_capital_usd):
        raise ValueError("total_initial_capital_usd must be finite and positive.")
    if usd_per_usdt <= 0.0 or not np.isfinite(usd_per_usdt):
        raise ValueError("usd_per_usdt must be finite and positive.")
    if len(aligned_navs) < 2:
        raise ValueError("aligned_navs must contain at least 2 valuation dates.")

    alp_nav = aligned_navs["alpaca_nav"].to_numpy(dtype=np.float64)
    bn_nav = aligned_navs["binance_nav"].to_numpy(dtype=np.float64)
    if not (np.isfinite(alp_nav).all() and np.isfinite(bn_nav).all()) or (alp_nav <= 0).any() or (bn_nav <= 0).any():
        raise ValueError("aligned_navs contains non-finite or non-positive NAV values.")

    alp_growth = alp_nav / alp_nav[0]
    bn_growth = bn_nav / bn_nav[0]
    results: list[dict[str, Any]] = []

    for bn_w0 in binance_weights:
        bn_w0 = float(bn_w0)
        if bn_w0 < 0.0 or bn_w0 > 1.0:
            raise ValueError(f"Invalid initial binance weight {bn_w0}; must be in [0, 1].")
        alp_w0 = 1.0 - bn_w0

        # Independent account compounding from initial allocations (zero cross-exchange transfers / rebalancing)
        alp_eq_usd = total_initial_capital_usd * alp_w0 * alp_growth
        bn_init_usdt = (total_initial_capital_usd * bn_w0) / usd_per_usdt
        bn_eq_usdt = bn_init_usdt * bn_growth
        bn_eq_usd = bn_eq_usdt * usd_per_usdt
        total_eq_usd = alp_eq_usd + bn_eq_usd

        # Derive portfolio daily returns, drawdown, VaR, and CVaR strictly from total combined equity
        comb_r = np.diff(total_eq_usd) / total_eq_usd[:-1]
        losses = -comb_r
        var95, cvar95 = _loss_tail(losses, 0.95)
        var99, cvar99 = _loss_tail(losses, 0.99)
        mdd = float(np.min(total_eq_usd / np.maximum.accumulate(total_eq_usd) - 1.0))

        # Track realized (drifting) weights over the entire natural-calendar horizon
        realized_alp_w = alp_eq_usd / total_eq_usd
        realized_bn_w = bn_eq_usd / total_eq_usd
        drift_bn = realized_bn_w - bn_w0

        results.append(
            {
                "initial_alpaca_weight": alp_w0,
                "initial_binance_weight": bn_w0,
                "final_alpaca_weight": float(realized_alp_w[-1]),
                "final_binance_weight": float(realized_bn_w[-1]),
                "min_binance_weight": float(np.min(realized_bn_w)),
                "max_binance_weight": float(np.max(realized_bn_w)),
                "final_binance_weight_drift_pp": float(drift_bn[-1] * 100.0),
                "max_abs_weight_drift_pp": float(np.max(np.abs(drift_bn)) * 100.0),
                "total_initial_capital_usd": float(total_initial_capital_usd),
                "final_alpaca_equity_usd": float(alp_eq_usd[-1]),
                "final_binance_equity_usd": float(bn_eq_usd[-1]),
                "final_total_equity_usd": float(total_eq_usd[-1]),
                "natural_calendar_days": int(len(comb_r)),
                "net_return": float(total_eq_usd[-1] / total_eq_usd[0] - 1.0),
                "max_dd": mdd,
                "daily_var_95": float(var95),
                "daily_cvar_95": float(cvar95),
                "daily_var_99": float(var99),
                "daily_cvar_99": float(cvar99),
                "risk_statistics_calendar": CROSS_MARKET_CALENDAR,
                "fx_conversion_assumption": CROSS_MARKET_VALUATION_SPEC["fx_conversion_assumption"],
                "usd_per_usdt": float(usd_per_usdt),
                "rebalancing_assumed": False,
                "cross_exchange_transfers_assumed": False,
                "shared_margin_assumed": False,
            }
        )
    return results


def verify_pr260_shared_capital_policy_contract(
    account_id: str,
    capital_usd: float,
    strategy_budgets: dict[str, float],
    fee_rate: float,
    max_symbol_usd: float,
) -> dict[str, Any]:
    """Instantiate and verify the real #260 PortfolioStore + CapitalPolicy(SHARED_SAME_DIRECTION) contract."""
    with tempfile.TemporaryDirectory(prefix="p10-04-pr260-policy-") as tmp:
        db_path = Path(tmp) / "shared_account.sqlite3"
        store = PortfolioStore(db_path)
        try:
            policy: CapitalPolicy = store.capital_policy
            policy.configure(
                account_id=account_id,
                currency="USD",
                budget=D(str(round(capital_usd, 2))),
                fee_rate=D(str(fee_rate)),
                mode=CapitalPolicyMode.SHARED_SAME_DIRECTION,
            )
            mode = policy.mode(account_id).value
            configured = policy.is_configured(account_id)
            total_budget = sum(strategy_budgets.values())
            budget_within_cap = total_budget <= capital_usd + 1e-6
            return {
                "contract_verified": bool(configured and mode == "SHARED_SAME_DIRECTION" and budget_within_cap),
                "account_id": account_id,
                "mode": mode,
                "account_budget_usd": capital_usd,
                "allocated_sum_usd": total_budget,
                "max_symbol_exposure_usd": max_symbol_usd,
                "strategy_budgets_usd": strategy_budgets,
            }
        finally:
            store.close()


def simulate_alpaca_shared_account(
    *,
    strategies: tuple[str, ...],
    allocation_weights: dict[str, float],
    market: MarketPanel,
    package_dir: Path,
    window_name: str,
    start: str,
    end: str,
    initial_capital: float = 300_000.0,
    fee_rate: float = 0.001,
    account_gross_cap: float = 0.90,
    max_symbol_weight: float = 0.35,
    volume_cap_ratio: float = 0.001,
    rebalance_deadband: float = 0.005,
    execution_order: str = "strategy_order",  # strategy_order | reverse_order | proportional_cash
    dynamic_regime_mode: bool = False,
    providers: dict[str, Any] | None = None,
    omit_sndk: bool = False,
) -> dict[str, Any]:
    """Run multiple strategies concurrently inside ONE Alpaca USD shared cash & inventory account."""
    full_index = market.close.index
    sessions = full_index[(full_index >= pd.Timestamp(start)) & (full_index <= pd.Timestamp(end))]

    # Instantiate signal providers for each active sleeve (or reuse cached read-only providers)
    if providers is None:
        providers = {
            s: build_signal_provider(s, market, package_dir=package_dir, omit_sndk=omit_sndk)
            for s in strategies
        }

    # Shared account state: ONE cash pool, per-strategy owned share slices summing to total account inventory
    account_cash = float(initial_capital)
    sleeve_cash = {s: float(initial_capital * allocation_weights[s]) for s in strategies}
    sleeve_held = {s: pd.Series(0.0, index=list(DEFAULT_TICKERS), dtype=np.float64) for s in strategies}
    last_week: tuple[int, int] | None = None

    # Precompute SP500TR 200d MA for research-only dynamic allocation mode (strictly causal)
    sp_ma200 = market.benchmark_close.rolling(200).mean()

    nav_series = []
    cash_series = []
    exp_series = []
    overlap_symbol_days = 0
    max_concurrent_overlap_symbols = 0
    peak_single_symbol_weight = 0.0
    cash_contention_events = 0
    volume_cap_shared_binding_events = 0
    symbol_limit_binding_events = 0
    gross_cap_binding_events = 0
    total_fees = 0.0
    total_fills = 0
    sleeve_nav_history: dict[str, list[float]] = {s: [] for s in strategies}

    for day in sessions:
        iso = day.isocalendar()
        week = (int(iso.year), int(iso.week))
        i_full = int(full_index.get_loc(day))
        previous = full_index[i_full - 1]

        if week != last_week:
            prices = market.close.loc[previous, list(DEFAULT_TICKERS)]
            volumes = market.volume.loc[previous, list(DEFAULT_TICKERS)]
            valid = np.isfinite(prices) & prices.gt(0.0)
            tradable = valid & np.isfinite(volumes) & volumes.ge(0.0)

            # Determine effective sleeve budget shares (fixed or research-only causal regime shift)
            if dynamic_regime_mode and set(strategies) == {"v69", "v179", "v182"}:
                sp_up = bool(
                    pd.notna(sp_ma200.at[previous])
                    and market.benchmark_close.at[previous] > sp_ma200.at[previous]
                )
                eff_alloc = {"v69": 0.20, "v179": 0.45, "v182": 0.35} if sp_up else {"v69": 0.50, "v179": 0.30, "v182": 0.20}
            else:
                eff_alloc = dict(allocation_weights)

            # Compute each strategy's independent target weights using its own owned holdings
            acct_held = sum(sleeve_held.values())
            acct_prev_val = float((acct_held * prices.where(tradable)).fillna(0.0).sum())
            acct_prev_nav = float(account_cash + acct_prev_val)

            sleeve_targets_acct: dict[str, pd.Series] = {}
            for s in strategies:
                raw_w = providers[s].targets(previous, prices, valid, sleeve_held[s])
                sleeve_targets_acct[s] = raw_w * eff_alloc[s]

            # Account-level risk budget enforcement (per-symbol cap & account gross cap)
            combined_target = sum(sleeve_targets_acct.values())
            for sym in DEFAULT_TICKERS:
                if combined_target[sym] > max_symbol_weight + 1e-12:
                    symbol_limit_binding_events += 1
                    scale = max_symbol_weight / float(combined_target[sym])
                    for s in strategies:
                        sleeve_targets_acct[s][sym] *= scale
            combined_target = sum(sleeve_targets_acct.values())
            if float(combined_target.sum()) > account_gross_cap + 1e-12:
                gross_cap_binding_events += 1
                scale = account_gross_cap / float(combined_target.sum())
                for s in strategies:
                    sleeve_targets_acct[s] *= scale

            # Build per-strategy sell & buy intents against shared account NAV & sleeve owned holdings
            strat_order = list(strategies) if execution_order != "reverse_order" else list(reversed(strategies))
            all_sells: list[tuple[str, str, float]] = []
            all_buys: list[tuple[str, str, float]] = []
            for s in strat_order:
                s_val = (sleeve_held[s] * prices.where(tradable)).fillna(0.0)
                for sym in DEFAULT_TICKERS:
                    if not tradable[sym]:
                        continue
                    cur_w = float(s_val[sym] / acct_prev_nav) if acct_prev_nav > 0 else 0.0
                    tgt_w = float(sleeve_targets_acct[s][sym])
                    if tgt_w == 0.0:
                        amt = float(-sleeve_held[s][sym])
                    elif abs(tgt_w - cur_w) >= rebalance_deadband * eff_alloc[s]:
                        amt = float((tgt_w - cur_w) * acct_prev_nav / prices[sym])
                    else:
                        continue
                    if abs(amt) <= 1e-8:
                        continue
                    if amt < 0.0:
                        all_sells.append((s, sym, abs(amt)))
                    else:
                        all_buys.append((s, sym, amt))

            # Enforce shared symbol liquidity cap (0.1% prior volume across all strategies combined)
            remaining_vol_cap = {sym: volume_cap_ratio * float(volumes[sym]) if tradable[sym] else 0.0 for sym in DEFAULT_TICKERS}

            # Phase A: Execute ALL sells first across all strategies (releasing shared cash)
            for s, sym, req_qty in all_sells:
                px = float(market.opening.at[day, sym])
                if not np.isfinite(px) or px <= 0.0:
                    continue
                cap_qty = remaining_vol_cap[sym]
                if req_qty > cap_qty + 1e-8:
                    volume_cap_shared_binding_events += 1
                qty = min(req_qty, float(sleeve_held[s][sym]), cap_qty)
                if qty <= 1e-8:
                    continue
                tv = qty * px
                fee = tv * fee_rate
                sleeve_held[s][sym] -= qty
                if abs(sleeve_held[s][sym]) < 1e-12:
                    sleeve_held[s][sym] = 0.0
                remaining_vol_cap[sym] -= qty
                account_cash += tv - fee
                sleeve_cash[s] += tv - fee
                total_fees += fee
                total_fills += 1

            # Phase B: Execute buys under shared account cash pool
            if all_buys:
                buy_notionals = []
                for s, sym, req_qty in all_buys:
                    px = float(market.opening.at[day, sym])
                    cap_qty = min(req_qty, remaining_vol_cap[sym])
                    buy_notionals.append((s, sym, cap_qty, px, cap_qty * px * (1.0 + fee_rate)))
                total_buy_cash_needed = sum(x[4] for x in buy_notionals)
                if total_buy_cash_needed > account_cash + 1e-6:
                    cash_contention_events += 1
                if execution_order == "proportional_cash" and total_buy_cash_needed > account_cash > 0.0:
                    cash_scale = account_cash / total_buy_cash_needed
                    for s, sym, cap_qty, px, _ in buy_notionals:
                        qty = min(cap_qty * cash_scale, remaining_vol_cap[sym])
                        if qty <= 1e-8 or not np.isfinite(px) or px <= 0.0:
                            continue
                        tv = qty * px
                        fee = tv * fee_rate
                        if tv + fee > account_cash:
                            qty = max(0.0, account_cash / (px * (1.0 + fee_rate)))
                            tv = qty * px
                            fee = tv * fee_rate
                        if qty <= 1e-8:
                            continue
                        sleeve_held[s][sym] += qty
                        remaining_vol_cap[sym] -= qty
                        account_cash -= tv + fee
                        sleeve_cash[s] -= tv + fee
                        total_fees += fee
                        total_fills += 1
                else:
                    for s, sym, req_qty in all_buys:
                        px = float(market.opening.at[day, sym])
                        if not np.isfinite(px) or px <= 0.0:
                            continue
                        cap_qty = remaining_vol_cap[sym]
                        if req_qty > cap_qty + 1e-8:
                            volume_cap_shared_binding_events += 1
                        max_aff = max(0.0, account_cash / (px * (1.0 + fee_rate)))
                        qty = min(req_qty, cap_qty, max_aff)
                        if qty <= 1e-8:
                            continue
                        tv = qty * px
                        fee = tv * fee_rate
                        sleeve_held[s][sym] += qty
                        remaining_vol_cap[sym] -= qty
                        account_cash -= tv + fee
                        sleeve_cash[s] -= tv + fee
                        total_fees += fee
                        total_fills += 1

            last_week = week

        # Mark-to-market shared account & measure overlap / concentration
        close_row = market.close.loc[day, list(DEFAULT_TICKERS)]
        acct_held = sum(sleeve_held.values())
        acct_marks = (acct_held * close_row).fillna(0.0)
        acct_val = float(acct_marks.sum())
        acct_nav = float(account_cash + acct_val)
        nav_series.append(acct_nav)
        cash_series.append(float(account_cash))
        exp_series.append(acct_val / acct_nav if acct_nav > 0 else 0.0)

        # Count symbols simultaneously held by >= 2 strategies
        owners_per_sym = sum((sleeve_held[s] > 1e-8).astype(int) for s in strategies)
        n_overlap = int((owners_per_sym >= 2).sum())
        if n_overlap > 0:
            overlap_symbol_days += 1
        max_concurrent_overlap_symbols = max(max_concurrent_overlap_symbols, n_overlap)
        if acct_nav > 0:
            peak_single_symbol_weight = max(peak_single_symbol_weight, float((acct_marks / acct_nav).max()))

        for s in strategies:
            s_val = float((sleeve_held[s] * close_row).fillna(0.0).sum())
            sleeve_nav_history[s].append(sleeve_cash[s] + s_val)

    daily = pd.DataFrame(
        {"nav": nav_series, "cash": cash_series, "exposure": exp_series},
        index=sessions,
    )
    daily["net_daily"] = daily["nav"].pct_change()
    daily.iloc[0, daily.columns.get_loc("net_daily")] = daily["nav"].iloc[0] / initial_capital - 1.0
    sp_daily = market.benchmark_close.pct_change(fill_method=None).reindex(sessions)
    metrics = compute_risk_metrics(daily["net_daily"], sp_daily)
    losses = -daily["net_daily"].to_numpy(dtype=np.float64)
    var95, cvar95 = _loss_tail(losses, 0.95)
    var99, cvar99 = _loss_tail(losses, 0.99)

    return {
        "window": window_name,
        "strategies": list(strategies),
        "allocation_weights": allocation_weights,
        "execution_order": execution_order,
        "dynamic_regime_mode": dynamic_regime_mode,
        "days": int(len(sessions)),
        "fills": int(total_fills),
        "fees": float(total_fees),
        "mean_exposure": float(daily["exposure"].mean()),
        "idle_cash_ratio": float(1.0 - daily["exposure"].mean()),
        "peak_exposure": float(daily["exposure"].max()),
        "peak_single_symbol_weight": float(peak_single_symbol_weight),
        "overlap_symbol_days": int(overlap_symbol_days),
        "overlap_symbol_day_fraction": float(overlap_symbol_days / max(1, len(sessions))),
        "max_concurrent_overlap_symbols": int(max_concurrent_overlap_symbols),
        "cash_contention_events": int(cash_contention_events),
        "shared_volume_cap_events": int(volume_cap_shared_binding_events),
        "symbol_limit_binding_events": int(symbol_limit_binding_events),
        "gross_cap_binding_events": int(gross_cap_binding_events),
        "daily_var_95": float(var95),
        "daily_cvar_95": float(cvar95),
        "daily_var_99": float(var99),
        "daily_cvar_99": float(cvar99),
        "daily_returns": daily["net_daily"],
        "daily_nav": daily["nav"],
        **metrics,
    }


def run_shared_account_study(
    config: ExternalValidationConfig,
    markets: dict[str, MarketPanel],
    base_results: dict[tuple[str, str, str], IndependentReplayResult],
) -> dict[str, Any]:
    """Execute complete Phase 6 multi-strategy shared account research (Alpaca + Binance isolated)."""
    pkg = Path(config.packages.v179_v182_dir)
    wmap = config.window_map()
    sa = config.shared_account

    # 1. Calibrate causal inverse-volatility risk budget weights strictly on dev_b (2019-2022)
    dev_vols = {}
    for s in ("v69", "v179", "v182"):
        r_dev = base_results[(s, "dev_b", "normal")].daily["net_daily"].to_numpy(dtype=np.float64)
        dev_vols[s] = float(r_dev.std(ddof=1) * math.sqrt(252.0))
    inv_v = {s: 1.0 / max(1e-6, v) for s, v in dev_vols.items()}
    inv_sum = sum(inv_v.values())
    risk_budget_weights = {s: inv_v[s] / inv_sum for s in ("v69", "v179", "v182")}

    # 2. Verify #260 public CapitalPolicy contract on SQLite store
    pr260_contract = verify_pr260_shared_capital_policy_contract(
        account_id="alpaca-paper-shared-research",
        capital_usd=sa.alpaca_initial_capital_usd,
        strategy_budgets={s: sa.alpaca_initial_capital_usd / 3.0 for s in ("v69", "v179", "v182")},
        fee_rate=sa.alpaca_fee_rate,
        max_symbol_usd=sa.alpaca_initial_capital_usd * sa.alpaca_max_symbol_weight,
    )

    # 3. Pairwise correlation, simultaneous loss, and lower-tail dependence between individual strategies
    tail_dependence: dict[str, Any] = {}
    for win_name in ("target", "twenty"):
        df_r = pd.DataFrame({s: base_results[(s, win_name, "normal")].daily["net_daily"] for s in ("v69", "v179", "v182")})
        corr = df_r.corr()
        # Lower 5% tail conditional correlation & joint loss fraction
        pairs_info = {}
        for a, b in (("v69", "v179"), ("v69", "v182"), ("v179", "v182")):
            qa, qb = df_r[a].quantile(0.05), df_r[b].quantile(0.05)
            both_loss = float(((df_r[a] < 0) & (df_r[b] < 0)).mean())
            both_tail = float(((df_r[a] <= qa) & (df_r[b] <= qb)).mean())
            tail_cond = both_tail / 0.05
            pairs_info[f"{a}_vs_{b}"] = {
                "daily_pearson_corr": float(corr.at[a, b]),
                "simultaneous_loss_day_fraction": both_loss,
                "joint_5pct_tail_exceedance_prob": both_tail,
                "empirical_lower_tail_dependence_lambda_L": float(tail_cond),
            }
        all3_loss = float(((df_r["v69"] < 0) & (df_r["v179"] < 0) & (df_r["v182"] < 0)).mean())
        tail_dependence[win_name] = {
            "all_three_simultaneous_loss_day_fraction": all3_loss,
            "pairs": pairs_info,
        }

    # 4. Run all Alpaca shared-account allocation schemes + compare against naive weighted sum
    schemes: list[tuple[str, tuple[str, ...], dict[str, float], bool]] = [
        ("equal_weight_3", ("v69", "v179", "v182"), {"v69": 1 / 3, "v179": 1 / 3, "v182": 1 / 3}, False),
        ("risk_budget_inv_vol_3", ("v69", "v179", "v182"), risk_budget_weights, False),
        ("fixed_20_40_40", ("v69", "v179", "v182"), {"v69": 0.20, "v179": 0.40, "v182": 0.40}, False),
        ("pair_v69_v179_50_50", ("v69", "v179"), {"v69": 0.50, "v179": 0.50}, False),
        ("pair_v69_v182_50_50", ("v69", "v182"), {"v69": 0.50, "v182": 0.50}, False),
        ("pair_v179_v182_50_50", ("v179", "v182"), {"v179": 0.50, "v182": 0.50}, False),
        ("dynamic_regime_research_only", ("v69", "v179", "v182"), {"v69": 1 / 3, "v179": 1 / 3, "v182": 1 / 3}, True),
    ]

    alpaca_rows: list[dict[str, Any]] = []
    alpaca_daily_navs: dict[str, pd.Series] = {}

    for win_name in ("target", "twenty"):
        win = wmap[win_name]
        market = markets[win_name]
        sp_daily = market.benchmark_close.pct_change(fill_method=None)
        cached_providers = {
            s: build_signal_provider(s, market, package_dir=pkg, omit_sndk=False)
            for s in ("v69", "v179", "v182")
        }
        for sname, strats, alloc, is_dyn in schemes:
            res = simulate_alpaca_shared_account(
                strategies=strats,
                allocation_weights=alloc,
                market=market,
                package_dir=pkg,
                window_name=win_name,
                start=win.start,
                end=win.end,
                initial_capital=sa.alpaca_initial_capital_usd,
                fee_rate=sa.alpaca_fee_rate,
                account_gross_cap=sa.alpaca_gross_cap,
                max_symbol_weight=sa.alpaca_max_symbol_weight,
                dynamic_regime_mode=is_dyn,
                providers={s: cached_providers[s] for s in strats},
            )
            res.pop("daily_returns")
            nav_shared = res.pop("daily_nav")
            if win_name == "target":
                alpaca_daily_navs[sname] = nav_shared

            # Naive independent weighted sum (explicitly contrasted against true shared account!)
            naive_r = sum(alloc[s] * base_results[(s, win_name, "normal")].daily["net_daily"] for s in strats)
            naive_m = compute_risk_metrics(naive_r, sp_daily.reindex(naive_r.index))
            res["scheme"] = sname
            res["naive_independent_weighted_cagr"] = naive_m["cagr"]
            res["naive_independent_weighted_max_dd"] = naive_m["max_dd"]
            res["naive_independent_weighted_sharpe"] = naive_m["sharpe"]
            res["shared_minus_naive_cagr_pp"] = ((res["cagr"] or 0.0) - (naive_m["cagr"] or 0.0)) * 100.0
            res["shared_minus_naive_mdd_pp"] = (res["max_dd"] - naive_m["max_dd"]) * 100.0
            alpaca_rows.append(res)

    # 5. Execution-order sensitivity on Alpaca shared account (strategy_order vs reverse_order vs proportional_cash)
    order_sensitivity: list[dict[str, Any]] = []
    win_t = wmap["target"]
    target_providers = {
        s: build_signal_provider(s, markets["target"], package_dir=pkg, omit_sndk=False)
        for s in ("v69", "v179", "v182")
    }
    for order_mode in ("strategy_order", "reverse_order", "proportional_cash"):
        r_ord = simulate_alpaca_shared_account(
            strategies=("v69", "v179", "v182"),
            allocation_weights={"v69": 1 / 3, "v179": 1 / 3, "v182": 1 / 3},
            market=markets["target"],
            package_dir=pkg,
            window_name="target",
            start=win_t.start,
            end=win_t.end,
            initial_capital=sa.alpaca_initial_capital_usd,
            fee_rate=sa.alpaca_fee_rate,
            account_gross_cap=sa.alpaca_gross_cap,
            max_symbol_weight=sa.alpaca_max_symbol_weight,
            execution_order=order_mode,
            providers=target_providers,
        )
        r_ord.pop("daily_returns")
        r_ord.pop("daily_nav")
        order_sensitivity.append(r_ord)

    # 6. Binance USD-M Independent Account (NVDAUSDT Grid) & Isolated Cross-Exchange Risk Aggregation
    binance_summary = _evaluate_binance_isolated_account_and_cross_exchange(
        config=config,
        alpaca_target_nav=alpaca_daily_navs.get("equal_weight_3"),
        alpaca_v179_nav=base_results[("v179", "target", "normal")].daily["nav"],
    )

    return {
        "pr260_shared_capital_policy_contract": pr260_contract,
        "dev_b_calibrated_risk_budget_weights": risk_budget_weights,
        "dev_b_strategy_annualized_vol": dev_vols,
        "strategy_tail_dependence": tail_dependence,
        "alpaca_shared_account_schemes": alpaca_rows,
        "alpaca_execution_order_sensitivity": order_sensitivity,
        "binance_isolated_and_cross_exchange": binance_summary,
    }


def _evaluate_binance_isolated_account_and_cross_exchange(
    *,
    config: ExternalValidationConfig,
    alpaca_target_nav: pd.Series | None,
    alpaca_v179_nav: pd.Series,
) -> dict[str, Any]:
    """Evaluate Binance USD-M NVDAUSDT grid account separately and compute isolated cross-exchange risk."""
    repo_root = Path(__file__).resolve().parents[4]
    bars_path = (
        Path(config.packages.binance_nvda_bars)
        if config.packages.binance_nvda_bars
        else repo_root / "configs" / "research" / "data" / "nvdausdt_1h_audited.parquet"
    )
    funding_path = (
        Path(config.packages.binance_nvda_funding)
        if config.packages.binance_nvda_funding
        else repo_root / "configs" / "research" / "data" / "nvdausdt_funding_audited.json"
    )

    if not bars_path.is_file():
        return {
            "status": "PUBLISHED_R11_EVIDENCE_ONLY_LOCAL_PARQUET_ABSENT",
            "account_isolation_policy": "STRICT_NO_CROSS_EXCHANGE_CASH_OR_MARGIN_SHARING",
            "binance_account_currency": "USDT",
            "alpaca_account_currency": "USD",
            "note": f"Audited Binance parquet not found at {bars_path}; refer to frozen P8-R11/P8-S02-F01 reports.",
        }

    bars = pd.read_parquet(bars_path)
    ts_col = next((c for c in ("open_time", "timestamp", "date") if c in bars.columns), None)
    if ts_col is not None:
        bars = bars.set_index(pd.to_datetime(bars[ts_col], utc=True)).sort_index()
    else:
        bars.index = pd.to_datetime(bars.index, utc=True)

    # Load funding events if available
    funding_events = 0
    mean_funding_bps_8h = None
    if funding_path.is_file():
        fdata = json.loads(funding_path.read_text(encoding="utf-8"))
        records = fdata if isinstance(fdata, list) else fdata.get("events", fdata.get("records", []))
        rates = [float(x.get("fundingRate", x.get("rate", 0.0))) for x in records if isinstance(x, dict)]
        funding_events = len(rates)
        mean_funding_bps_8h = float(np.mean(rates) * 1e4) if rates else 0.0

    # Simulate transparent 1x long-only NVDAUSDT grid proxy on 1h UTC closes -> daily UTC EOD NAV
    initial_usdt = float(config.shared_account.binance_initial_capital_usdt)
    grid_nav_daily = _simulate_simple_nvda_grid_daily(
        bars["close"],
        initial_capital_usdt=initial_usdt,
        fee_slip_bps=5.0,
    )
    # Also compute daily UTC EOD NVDAUSDT spot price series for NAV-first spot correlation
    nvda_daily_close = bars["close"].resample("1D").last().dropna()

    # Align raw NAVs FIRST on unified natural UTC calendar (23:59:59 UTC), preserving 100% of Binance weekend/holiday PnL
    ref_alpaca_nav = alpaca_target_nav if alpaca_target_nav is not None else alpaca_v179_nav
    aligned, align_meta = align_cross_market_navs(
        ref_alpaca_nav,
        grid_nav_daily,
        binance_initial_nav=initial_usdt,
    )

    # Align NVDAUSDT spot price onto the same natural calendar (anchor at bar 0 open/close if pre-window anchor used)
    nvda_spot_aligned, _ = align_cross_market_navs(
        ref_alpaca_nav,
        nvda_daily_close,
        binance_initial_nav=float(bars["open"].iloc[0]) if "open" in bars.columns else float(bars["close"].iloc[0]),
    )

    eval_df = aligned.iloc[1:]
    alp_nat_ret = eval_df["alpaca_daily_ret"]
    bn_nat_ret = eval_df["binance_daily_ret"]
    nvda_nat_ret = nvda_spot_aligned.iloc[1:]["binance_daily_ret"]

    # Correlations on BOTH:
    # (1) Unified natural calendar (Alpaca 0% on confirmed closed days, Binance 24/7 daily returns)
    corr_nat_grid = float(alp_nat_ret.corr(bn_nat_ret)) if len(eval_df) > 5 and bn_nat_ret.std() > 0 else None
    corr_nat_nvda = float(alp_nat_ret.corr(nvda_nat_ret)) if len(eval_df) > 5 and nvda_nat_ret.std() > 0 else None

    # (2) NYSE open-session intervals (accumulated multi-day NAV ratios so weekend/holiday Binance moves roll into Monday)
    open_mask = ~aligned["alpaca_market_closed"]
    open_navs = aligned.loc[open_mask, ["alpaca_nav", "binance_nav"]]
    open_spot = nvda_spot_aligned.loc[open_mask, ["alpaca_nav", "binance_nav"]]
    open_alp_ret = open_navs["alpaca_nav"].pct_change().dropna()
    open_bn_ret = open_navs["binance_nav"].pct_change().dropna()
    open_nvda_ret = open_spot["binance_nav"].pct_change().dropna()
    corr_session_grid = float(open_alp_ret.corr(open_bn_ret)) if len(open_alp_ret) > 5 and open_bn_ret.std() > 0 else None
    corr_session_nvda = float(open_alp_ret.corr(open_nvda_ret)) if len(open_alp_ret) > 5 and open_nvda_ret.std() > 0 else None

    # Standalone Binance grid metrics on the COMPLETE natural calendar (zero dropped weekend/holiday PnL)
    g_nav = aligned["binance_nav"].to_numpy(dtype=np.float64)
    g_ret = bn_nat_ret.to_numpy(dtype=np.float64)
    g_losses = -g_ret
    g_var95, g_cvar95 = _loss_tail(g_losses, 0.95)
    g_var99, g_cvar99 = _loss_tail(g_losses, 0.99)
    g_mdd = float(np.min(g_nav / np.maximum.accumulate(g_nav) - 1.0))

    # Standalone Alpaca metrics over the exact same natural-calendar window
    a_nav = aligned["alpaca_nav"].to_numpy(dtype=np.float64)
    a_ret = alp_nat_ret.to_numpy(dtype=np.float64)
    a_var95, a_cvar95 = _loss_tail(-a_ret, 0.95)
    a_var99, a_cvar99 = _loss_tail(-a_ret, 0.99)
    a_mdd = float(np.min(a_nav / np.maximum.accumulate(a_nav) - 1.0))

    # Isolated cross-exchange buy-and-hold portfolio aggregation (NO daily rebalancing, NO shared margin)
    agg_results = aggregate_isolated_cross_exchange_portfolios(
        aligned,
        binance_weights=(0.0, 0.05, 0.10, 0.20),
        total_initial_capital_usd=float(config.shared_account.alpaca_initial_capital_usd),
        usd_per_usdt=1.0,
    )

    return {
        "status": "EVALUATED_ON_AUDITED_LOCAL_BINANCE_DATA",
        "account_isolation_policy": "STRICT_NO_CROSS_EXCHANGE_CASH_OR_MARGIN_SHARING",
        "valuation_and_calendar_spec": align_meta,
        "binance_bars_count": int(len(bars)),
        "binance_date_range": [str(bars.index.min()), str(bars.index.max())],
        "funding_events": funding_events,
        "mean_funding_bps_8h": mean_funding_bps_8h,
        "aligned_natural_calendar_days": align_meta["natural_calendar_return_days"],
        "aligned_alpaca_open_sessions": align_meta["alpaca_open_session_days"],
        "aligned_alpaca_confirmed_closed_days": align_meta["alpaca_confirmed_closed_days"],
        "overlap_range": [align_meta["return_start_date"], align_meta["return_end_date"]],
        "alpaca_shared_standalone_aligned": {
            "natural_calendar_days": int(len(a_ret)),
            "net_return": float(a_nav[-1] / a_nav[0] - 1.0),
            "max_dd": a_mdd,
            "daily_var_95": float(a_var95),
            "daily_cvar_95": float(a_cvar95),
            "daily_var_99": float(a_var99),
            "daily_cvar_99": float(a_cvar99),
            "risk_statistics_calendar": CROSS_MARKET_CALENDAR,
        },
        "binance_grid_standalone_overlap": {
            "natural_calendar_days": int(len(g_ret)),
            "net_return": float(g_nav[-1] / g_nav[0] - 1.0),
            "max_dd": g_mdd,
            "daily_var_95": float(g_var95),
            "daily_cvar_95": float(g_cvar95),
            "daily_var_99": float(g_var99),
            "daily_cvar_99": float(g_cvar99),
            "omitted_trading_days": 0,
            "pnl_reconciliation_error": align_meta["binance_pnl_reconciliation_error"],
            "risk_statistics_calendar": CROSS_MARKET_CALENDAR,
            "r11_reference_note": "Consistent with P8-R11/P8-S02-F01 finding: NVDAUSDT 1x grid suffers negative net return under realistic breakout exit friction and fee drag.",
        },
        "cross_exchange_correlations": {
            "natural_calendar_alpaca_vs_binance_grid_daily_corr": corr_nat_grid,
            "natural_calendar_alpaca_vs_binance_nvda_spot_daily_corr": corr_nat_nvda,
            "nyse_session_cumulated_alpaca_vs_binance_grid_corr": corr_session_grid,
            "nyse_session_cumulated_alpaca_vs_binance_nvda_spot_corr": corr_session_nvda,
            "nyse_session_interval_count": int(len(open_alp_ret)),
        },
        "isolated_cross_exchange_aggregation": agg_results,
    }


def _simulate_simple_nvda_grid_daily(
    closes_1h: pd.Series,
    *,
    initial_capital_usdt: float = 10.0,
    fee_slip_bps: float = 5.0,
) -> pd.Series:
    """Transparent 1x long-only single-band NVDAUSDT grid proxy on 1h UTC closes (returns daily UTC EOD NAV)."""
    cost = fee_slip_bps / 10_000.0
    cash = float(initial_capital_usdt)
    qty = 0.0
    anchor = float(closes_1h.iloc[0])
    lower, upper = anchor * 0.99, anchor * 1.01
    nav_1h = []
    for px in closes_1h.to_numpy(dtype=np.float64):
        if qty == 0.0 and px <= lower:
            buy_notional = min(cash / (1.0 + cost), 0.04 * px)
            if buy_notional >= 5.0:
                q = buy_notional / px
                cash -= q * px * (1.0 + cost)
                qty += q
        elif qty > 0.0 and (px >= upper or px < lower * 0.99):
            # Take profit at upper or F11 breakout exit below lower band
            cash += qty * px * (1.0 - cost)
            qty = 0.0
            anchor = px
            lower, upper = anchor * 0.99, anchor * 1.01
        nav_1h.append(cash + qty * px)
    s = pd.Series(nav_1h, index=closes_1h.index)
    return s.resample("1D").last().dropna()
