"""Run only against already frozen, authorized private inputs. NO LIVE MONEY."""

import argparse
import json
from pathlib import Path
import subprocess

import pandas as pd
from pyarrow import ArrowException

from ..contract import ExploratoryContractError
from ..runner import FACTOR_NAMES
from .delivery import (
    MARKERS,
    audit_destination,
    code_evidence,
    publish_audit,
    verify_audit,
    write_receipt,
)
from .inputs import load_frozen, require_equal
from .quintiles import compare_quintiles
from .statistics import infer_panel


def regime_intervals(panel, regimes):
    rows = []
    groups = regimes.ne(regimes.shift()).cumsum()
    for number in groups.unique():
        selected = groups.eq(number)
        dates = panel.index[selected]
        for factor in panel:
            valid = int(panel.loc[selected, factor].notna().sum())
            rows.append(
                dict(
                    interval_id=int(number),
                    state=regimes.loc[selected].iloc[0],
                    start=dates.min(),
                    end=dates.max(),
                    signal_dates=len(dates),
                    factor=factor,
                    valid_dates=valid,
                    coverage=valid / float(len(dates)),
                )
            )
    return pd.DataFrame(rows)


def verify_original_quintiles(daily, spread, original):
    """Compare A with the immutable aggregate; never reverse-engineer members."""
    original = original.loc[original.scope.eq("ALL")].copy()
    actual = daily.loc[
        daily.A_formed,
        ["date", "factor", "quantile", "A_mean", "A_members", "A_observable_members"],
    ].rename(
        columns={
            "A_mean": "future_return",
            "A_members": "observations",
            "A_observable_members": "labeled_observations",
        }
    )
    original_q = original.loc[original["quantile"].ne("Q5-Q1"), actual.columns]
    sort = ["date", "factor", "quantile"]
    require_equal(
        actual.sort_values(sort), original_q.sort_values(sort), "original A quintiles"
    )
    expected = original.loc[
        original["quantile"].eq("Q5-Q1"), ["date", "factor", "future_return"]
    ]
    actual = spread.loc[
        spread.A_Q5_minus_Q1.notna(), ["date", "factor", "A_Q5_minus_Q1"]
    ].rename(columns={"A_Q5_minus_Q1": "future_return"})
    require_equal(
        actual.sort_values(["date", "factor"]),
        expected.sort_values(["date", "factor"]),
        "original A Q5-Q1",
    )


def run(manifest, private_run, publication, output_root):
    from .report import write_report

    code = {"verification_complete": False}
    try:
        code = code_evidence()
        frame, panel, regimes, dates, evidence, original = load_frozen(
            manifest, private_run, publication
        )
        descriptors, inference, state_means, comparisons = infer_panel(
            panel, regimes, FACTOR_NAMES
        )
        daily, spread, monthly, membership = compare_quintiles(frame, FACTOR_NAMES)
        verify_original_quintiles(daily, spread, original)
    except (
        ExploratoryContractError,
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        ArrowException,
        subprocess.CalledProcessError,
    ) as exc:
        evidence = {"verification_complete": False}
        reasons = [
            "required frozen private input or statistical contract failed: " + str(exc)
        ]
        destination, exists = audit_destination(
            output_root, dict(**evidence, reasons=reasons), code
        )
        if not exists:
            (destination / "report.md").write_text(
                "# BLOCKED：统计审计必要输入/合同未满足\n\n"
                + " · ".join(MARKERS)
                + "\n\n"
                + "\n".join(reasons)
                + "\n\n必要输入：冻结manifest及全部源哈希、50股RAW/HFQ/基准/实际日历/名单；v2私有运行及证券级因子标签；对应公开v2收据及每日IC/覆盖/分位；精确冻结计算代码。不得重下载或合成补齐。\n",
                encoding="utf-8",
            )
            write_receipt(destination, "BLOCKED", evidence, code, reasons)
        return "BLOCKED", destination
    destination, exists = audit_destination(output_root, evidence, code)
    if not exists:
        tables = {
            "daily_input_audit.csv": dates,
            "rankic_descriptive.csv": descriptors,
            "rankic_inference.csv": inference,
            "rankic_method_sensitivity.csv": method_sensitivity(inference),
            "regime_means.csv": state_means,
            "regime_comparisons.csv": comparisons,
            "regime_intervals.csv": regime_intervals(panel, regimes),
            "quintile_daily.csv": daily,
            "quintile_spread.csv": spread,
            "quintile_monthly.csv": monthly,
        }
        write_report(destination, tables, evidence, panel, regimes)
        membership.to_csv(destination / "private_quintile_membership.csv", index=False)
        write_receipt(destination, "SUCCESS", evidence, code, [])
    verify_audit(destination / "receipt.json")
    return "SUCCESS", destination


def method_sensitivity(inference):
    wide = inference.pivot(
        index="factor",
        columns="method",
        values=["se", "p_two_sided", "ci_low", "ci_high"],
    )
    wide.columns = [method + "_" + metric for metric, method in wide.columns]
    wide["HAC20_over_HAC5_se"] = wide.HAC20_se / wide.HAC5_se
    wide["MBB20_over_HAC20_se"] = wide.MBB20_se / wide.HAC20_se
    return wide.reset_index()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="EXPLORATORY statistical audit; frozen inputs only; NO LIVE MONEY"
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--private-run", type=Path)
    parser.add_argument("--publication", type=Path)
    parser.add_argument("--output", type=Path, default=Path("results/exploratory"))
    parser.add_argument("--publish-to", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args(argv)
    if args.verify:
        receipt = verify_audit(args.verify, public=args.public)
        print(json.dumps(dict(verified=True, status=receipt["status"])))
        return 0
    if not all((args.manifest, args.private_run, args.publication)):
        parser.error("--manifest, --private-run and --publication required")
    status, destination = run(
        args.manifest, args.private_run, args.publication, args.output
    )
    if args.publish_to:
        publish_audit(destination, args.publish_to)
    print(
        json.dumps(
            dict(
                status=status,
                private_output=str(destination),
                public_output=str(args.publish_to) if args.publish_to else None,
            )
        )
    )
    return 0 if status == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
