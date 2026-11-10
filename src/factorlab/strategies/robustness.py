"""Independent-account robustness tables; no portfolio construction or admission."""
from copy import deepcopy

import pandas as pd

from factorlab.errors import SchemaError
from factorlab.strategies.real_comparison import NAMES, RETURN_BASIS, compare
from factorlab.strategies.technical_research import SAFETY

BLOCKED = "P6_R01_BLOCKED_DATA"
WAITING = "WAITING_FOR_P6_D01_INPUT"


def blocked_report(configuration):
    """Describe pending input without creating empirical metrics."""
    return dict(schema="FACTORLAB_ROBUSTNESS_V1", status=BLOCKED, input_status=WAITING,
                scope="EXPLORATORY_REAL_DATA", safety=SAFETY, return_basis=RETURN_BASIS,
                total_return=None, total_return_status="TOTAL_RETURN_UNAVAILABLE",
                candidate_universe=configuration["candidates"], years=configuration["years"],
                qualified_windows=[], observations=[], aggregates=None,
                real_accounts_run=0)


def software_batch(samples, template):
    """Run synthetic interface samples without granting real evidence."""
    return account_batch(samples, template, source_kind="SYNTHETIC")


def account_batch(samples, template, *, source_kind):
    """Evaluate frozen independent accounts through the existing comparator.

    samples carry bars/calendar/window/industry; source_kind labels provenance,
    not admission. Real callers must use the pinned D01 consumer.
    """
    samples = list(samples)
    keys = [(s["symbol"], s["year"]) for s in samples]
    if not samples or len(keys) != len(set(keys)):
        raise SchemaError("nonempty unique symbol-year samples required")
    rows, results = [], {}
    for sample in samples:
        config = deepcopy(template)
        config["common"].update(symbol=sample["symbol"], start=sample["start"], end=sample["end"])
        config["warmup_start"] = sample["warmup_start"]
        result = compare(sample["bars"], sample["calendar"], config, source_kind=source_kind)
        results[(sample["symbol"], sample["year"])] = result
        for name in NAMES:
            nav = result["legs"][name]["nav"]
            trades = result["legs"][name]["trades"]
            stats = result["summary"][name]
            rows.append(dict(symbol=sample["symbol"], year=sample["year"],
                industry=sample["industry"], strategy=name, **stats,
                average_capital_exposure=float((nav.market_value / nav.portfolio_value).mean()),
                holding_sessions=int(nav.shares.gt(0).sum()), sessions=len(nav),
                cost_to_initial_capital=stats["total_transaction_cost"] / config["common"]["initial_cash"],
                traded_notional=float(trades.trade_notional.sum())))
    observations = pd.DataFrame(rows)
    return dict(status="SOFTWARE_ONLY" if source_kind == "SYNTHETIC" else "REAL_ACCOUNTS_CALCULATED", source_kind=source_kind, safety=SAFETY,
                return_basis=RETURN_BASIS, observations=observations,
                tables=summarize(observations), results=results)


def _distribution(frame, groups):
    return frame.groupby(groups, sort=True).agg(
        accounts=("cumulative_return", "size"), securities=("symbol", "nunique"),
        mean_return=("cumulative_return", "mean"), median_return=("cumulative_return", "median"),
        minimum_return=("cumulative_return", "min"), maximum_return=("cumulative_return", "max"),
        q25_return=("cumulative_return", lambda x: x.quantile(.25)),
        q75_return=("cumulative_return", lambda x: x.quantile(.75)),
        profitable_account_fraction=("profitable", "mean"),
        outperform_account_fraction=("outperform", "mean"),
        mean_exposure=("average_capital_exposure", "mean"),
        mean_trades=("number_of_trades", "mean"),
        mean_cost_to_initial_capital=("cost_to_initial_capital", "mean"),
        mean_turnover=("portfolio_turnover", "mean")).reset_index()


def summarize(observations):
    """Descriptive account summaries, never portfolio Sharpe or significance.

    Security-level proportions use each security's arithmetic mean annual return
    over its available windows; year coverage is retained explicitly.
    """
    frame = observations.copy()
    if frame.empty or frame.duplicated(["symbol", "year", "strategy"]).any():
        raise SchemaError("nonempty unique account observations required")
    counts = frame.groupby(["symbol", "year"]).strategy.agg(lambda x: set(x))
    if not counts.map(lambda x: x == set(NAMES)).all():
        raise SchemaError("each account window requires all six mandates")
    frame["profitable"] = frame.cumulative_return.gt(0)
    frame["outperform"] = frame.excess_return.gt(0)
    securities = frame.groupby(["strategy", "symbol"], sort=True).agg(
        years=("year", "nunique"), mean_annual_return=("cumulative_return", "mean"),
        mean_annual_excess=("excess_return", "mean"),
        profitable_year_fraction=("profitable", "mean"),
        outperform_year_fraction=("outperform", "mean")).reset_index()
    securities["profitable"] = securities.mean_annual_return.gt(0)
    securities["outperform"] = securities.mean_annual_excess.gt(0)
    proportions = securities.groupby("strategy").agg(
        securities=("symbol", "size"), profitable_security_fraction=("profitable", "mean"),
        outperform_security_fraction=("outperform", "mean"),
        equal_security_mean_annual_return=("mean_annual_return", "mean")).reset_index()
    omissions = []
    for strategy, group in securities.groupby("strategy"):
        for symbol in group.symbol:
            remaining = group.loc[group.symbol.ne(symbol), "mean_annual_return"]
            omissions.append(dict(strategy=strategy, omitted_symbol=symbol,
                remaining_securities=len(remaining),
                mean_annual_return=None if remaining.empty else float(remaining.mean()),
                change_from_all=None if remaining.empty else float(remaining.mean() - group.mean_annual_return.mean())))
    return dict(overall=_distribution(frame, ["strategy"]),
                annual=_distribution(frame, ["strategy", "year"]),
                industry=_distribution(frame, ["strategy", "industry"]),
                annual_industry=_distribution(frame, ["strategy", "year", "industry"]),
                security_consistency=securities, security_proportions=proportions,
                leave_one_security_out=pd.DataFrame(omissions),
                exposure_cost_returns=frame[["symbol", "year", "strategy", "cumulative_return",
                    "excess_return", "average_capital_exposure", "maximum_drawdown",
                    "number_of_trades", "portfolio_turnover", "cost_to_initial_capital",
                    "rejection_count", "holding_time_ratio"]])
