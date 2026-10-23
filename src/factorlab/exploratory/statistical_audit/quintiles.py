"""Independent label-availability selection diagnostic; no portfolio claim."""

import numpy as np
import pandas as pd

from ..contract import ExploratoryContractError
from ..runner import MINIMUM_CROSS_SECTION, TARGET


def compare_quintiles(frame, factors):
    """Retain source row order for rank(method='first') ties, as Golden does.

    Public result contains aggregate counts and returns. Security memberships
    stay in the separate private table. A's minimum is 20 labeled factors;
    B's minimum is 20 contemporaneous factors, even with no future labels.
    """
    if frame.duplicated(["date", "symbol"]).any():
        raise ExploratoryContractError("duplicate security-date input")
    daily, membership = [], []
    for date, cross in frame.groupby("date", sort=True):
        for factor in factors:
            available = cross.loc[
                np.isfinite(cross[factor]), ["symbol", factor, TARGET]
            ].copy()
            labeled = np.isfinite(available[TARGET])
            universe = len(available)
            missing_rate = float((~labeled).mean()) if universe else np.nan
            subsets = {"A": available.loc[labeled], "B": available}
            buckets = {}
            for method, subset in subsets.items():
                buckets[method] = (
                    pd.qcut(
                        subset[factor].rank(method="first"), 5, labels=False
                    ).astype(int)
                    + 1
                    if len(subset) >= MINIMUM_CROSS_SECTION
                    else pd.Series(index=subset.index, dtype=float)
                )
                for idx, q in buckets[method].dropna().items():
                    membership.append(
                        dict(
                            date=date,
                            factor=factor,
                            method=method,
                            symbol=subset.at[idx, "symbol"],
                            quantile="Q%d" % q,
                            label_observable=bool(np.isfinite(subset.at[idx, TARGET])),
                        )
                    )
            for q in range(1, 6):
                groups, members, observable = {}, {}, {}
                for method in ("A", "B"):
                    group = subsets[method].loc[buckets[method].eq(q)]
                    realized = group.loc[np.isfinite(group[TARGET])]
                    groups[method] = group
                    members[method] = set(group.symbol)
                    observable[method] = set(realized.symbol)
                row = dict(
                    date=date,
                    factor=factor,
                    quantile="Q%d" % q,
                    eligible_factor_count=universe,
                    observable_factor_count=int(labeled.sum()),
                    label_missing_rate=missing_rate,
                )
                for method in ("A", "B"):
                    group = groups[method]
                    valid = np.isfinite(group[TARGET])
                    row.update(
                        {
                            method + "_formed": len(subsets[method])
                            >= MINIMUM_CROSS_SECTION,
                            method + "_members": len(members[method]),
                            method + "_observable_members": len(observable[method]),
                            method
                            + "_mean": (
                                float(group.loc[valid, TARGET].mean())
                                if valid.any()
                                else np.nan
                            ),
                            method
                            + "_bucket_missing_rate": (
                                1 - len(observable[method]) / float(len(group))
                                if len(group)
                                else np.nan
                            ),
                        }
                    )
                row["observable_members_changed"] = len(
                    observable["A"] ^ observable["B"]
                )
                row["all_members_changed"] = len(members["A"] ^ members["B"])
                row["observable_member_overlap"] = len(
                    observable["A"] & observable["B"]
                )
                row["B_minus_A_mean"] = row["B_mean"] - row["A_mean"]
                daily.append(row)
    daily = pd.DataFrame(daily)
    spread = daily.loc[
        daily["quantile"].eq("Q1"),
        [
            "date",
            "factor",
            "label_missing_rate",
            "eligible_factor_count",
            "observable_factor_count",
        ],
    ].copy()
    for method in ("A", "B"):
        tails = daily.pivot(
            index=["date", "factor"], columns="quantile", values=method + "_mean"
        )
        spread[method + "_Q5_minus_Q1"] = (
            (tails.Q5 - tails.Q1)
            .reindex(pd.MultiIndex.from_frame(spread[["date", "factor"]]))
            .to_numpy()
        )
    spread["B_minus_A_spread"] = spread.B_Q5_minus_Q1 - spread.A_Q5_minus_Q1
    monthly = daily.copy()
    monthly["month"] = monthly.date.dt.strftime("%Y-%m")
    monthly = (
        monthly.groupby(["factor", "month", "quantile"])
        .agg(
            paired_dates=("B_minus_A_mean", "count"),
            mean_B_minus_A=("B_minus_A_mean", "mean"),
            max_abs_difference=("B_minus_A_mean", lambda x: x.abs().max()),
            mean_label_missing_rate=("label_missing_rate", "mean"),
            changed_member_dates=(
                "observable_members_changed",
                lambda x: int(x.gt(0).sum()),
            ),
        )
        .reset_index()
    )
    return daily, spread, monthly, pd.DataFrame(membership)
