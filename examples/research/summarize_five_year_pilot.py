"""Publish coverage metadata and standalone figures; never export vendor prices.

Inputs remain private. The output directory must be new. This produces an audit
summary, not a FactorLab dataset, factor evaluation or financial qualification.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from qbot.research.data.tdx import audit_day, safe_path
from qbot.research.data.tdx_staging import write_new
from qbot.research.exploratory.contract import sha256_file
from qbot.research.exploratory.five_year import verify_pilot
from examples.research.five_year_supplier_probe import response_rows


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def coverage_table(bars, calendar, symbols, start, end):
    """Monthly numerator/denominator on the actual observed benchmark calendar."""
    dates = pd.DatetimeIndex(calendar)
    dates = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if dates.empty or dates.has_duplicates:
        raise ValueError("unique nonempty actual calendar required")
    rows = []
    for symbol in symbols:
        stock = bars.loc[bars.symbol.eq(symbol)].set_index("date").reindex(dates)
        observed = stock[[prefix + name for prefix in ("raw_", "adj_")
                          for name in ("open", "high", "low", "close")]].notna().all(axis=1)
        for month in dates.to_period("M").unique():
            chosen = dates.to_period("M") == month
            expected, count = int(chosen.sum()), int(observed.to_numpy()[chosen].sum())
            rows.append({"symbol": symbol, "month": str(month), "expected_actual_sessions": expected,
                         "paired_observed_sessions": count, "missing_sessions": expected - count,
                         "coverage_ratio": count / expected, "missing_cause": "UNKNOWN_UNVERIFIED"})
    return pd.DataFrame(rows)


def figures(monthly, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.ticker import PercentFormatter

    symbols = list(dict.fromkeys(monthly.symbol))
    months = sorted(monthly.month.unique())
    values = monthly.pivot(index="symbol", columns="month", values="coverage_ratio").reindex(index=symbols, columns=months)
    palette = LinearSegmentedColormap.from_list("coverage", ["#f1f3f5", "#86b5cd", "#155678"])
    fig, ax = plt.subplots(figsize=(18, 6))
    mark = ax.imshow(values, aspect="auto", vmin=0, vmax=1, cmap=palette, interpolation="nearest")
    ticks = sorted(set([0, len(months) - 1] + list(range(0, len(months), 4))))
    ax.set_xticks(ticks, [months[i] for i in ticks], fontsize=10)
    ax.set_yticks(range(len(symbols)), symbols, fontsize=10)
    ax.set_title("RAW + vendor HFQ coverage on observed benchmark sessions", loc="left", fontsize=15, pad=18)
    ax.set_xlabel("2021-09-13 to 2026-09-11; first/last months are partial", labelpad=12)
    bar = fig.colorbar(mark, ax=ax, pad=0.02, fraction=0.025)
    bar.ax.yaxis.set_major_formatter(PercentFormatter(1))
    bar.set_label("Observed / expected sessions")
    fig.text(0.09, 0.02, "Frozen engineering sample. Missing cells keep NaN prices; causes are unverified. No PIT or HFQ certification.", fontsize=10)
    fig.subplots_adjust(left=0.09, right=0.93, top=0.88, bottom=0.17)
    fig.savefig(output / "coverage-monthly.png", dpi=160)
    plt.close(fig)

    annual = monthly.assign(year=monthly.month.str[:4]).groupby(["symbol", "year"], sort=False)[
        ["expected_actual_sessions", "paired_observed_sessions", "missing_sessions"]].sum().reset_index()
    annual["missing_ratio"] = annual.missing_sessions / annual.expected_actual_sessions
    years = sorted(annual.year.unique())
    gaps = annual.pivot(index="symbol", columns="year", values="missing_ratio").reindex(index=symbols, columns=years)
    fig, ax = plt.subplots(figsize=(10, 6))
    mark = ax.imshow(gaps, aspect="auto", vmin=0, vmax=1, cmap=palette, interpolation="nearest")
    ax.set_xticks(range(len(years)), years, fontsize=11)
    ax.set_yticks(range(len(symbols)), symbols, fontsize=10)
    ax.set_title("Missing RAW + vendor HFQ observations", loc="left", fontsize=14, pad=18)
    for i in range(len(symbols)):
        for j in range(len(years)):
            value = float(gaps.iloc[i, j])
            ax.text(j, i, "%.1f%%" % (value * 100), ha="center", va="center",
                    color="white" if value > 0.65 else "#14222b", fontsize=10)
    fig.colorbar(mark, ax=ax, pad=0.025, fraction=0.035, format=PercentFormatter(1)).set_label("Missing / expected sessions")
    fig.text(0.14, 0.03, "2021 and 2026 are partial years. Full-market calendar denominator; not a listing eligibility rate.", fontsize=9)
    fig.subplots_adjust(left=0.14, right=0.89, top=0.88, bottom=0.12)
    fig.savefig(output / "missing-annual.png", dpi=160)
    plt.close(fig)
    return annual


def replay_supplier_coverage(supplier, supplier_root, calendar):
    """Recheck existing bytes without HTTP; do not overwrite original failures."""
    actual = set(pd.DatetimeIndex(calendar).strftime("%Y-%m-%d"))
    expected = {day for day in actual if "2021-09-13" <= day <= "2026-09-11"}
    output = []
    for item in supplier["attempts"]:
        record = {key: item.get(key) for key in ("vendor", "symbol", "basis", "status", "response_sha256",
                  "response_bytes", "requested_at", "retrieved_at", "http_status", "elapsed_seconds",
                  "url", "representation", "parser_version", "error_type", "reason")}
        record.update(original_status=item["status"], qualification="UNVERIFIED",
                      validated_fields=["date", "open", "high", "low", "close"],
                      quantity_fields="native observations only; volume/amount/turnover units not independently verified")
        if "native_file" in item:
            path = safe_path(supplier_root / item["native_file"], supplier_root)
            content = path.read_bytes()
            try:
                if item["vendor"] == "TDX":
                    market = item["symbol"].split(".")[1].lower()
                    filename = market + item["symbol"][:6] + ".day"
                    audit = audit_day(supplier_root / filename, market, supplier_root,
                                      "2021-03-08", "2026-09-11", source_bytes=content)
                    if not audit.convertible or audit.dates is None:
                        raise ValueError("TDX candidate decode failed")
                    dates = list(audit.dates.strftime("%Y-%m-%d"))
                else:
                    dates = response_rows(content, item["vendor"], item["symbol"], item["basis"])
                chosen = {day for day in dates if "2021-09-13" <= day <= "2026-09-11"}
                invalid = chosen - expected
                if invalid:
                    raise ValueError("source observations outside actual benchmark calendar")
                record.update(replay_status="OBSERVED" if chosen else "EMPTY",
                              research_expected_sessions=len(expected), observed_research_sessions=len(chosen),
                              missing_research_sessions=len(expected - chosen),
                              first_observed_date=min(chosen) if chosen else None,
                              last_observed_date=max(chosen) if chosen else None,
                              noncalendar_research_dates=len(invalid))
            except ValueError as exc:
                record.update(replay_status="SOURCE_SHAPE_UNVERIFIED", replay_reason=str(exc))
        else:
            record["replay_status"] = "SOURCE_BYTES_UNAVAILABLE"
        output.append(record)
    return output


def verified_benchmark_provenance(checked):
    """Publish the verifier's classification, never a legacy self-assertion."""
    manifest = checked["manifest"]
    if manifest["schema"] == "FIVE_YEAR_PILOT_MANIFEST_V3":
        return manifest["benchmark_provenance"]
    return {"status": checked["verification"]["calendar_origin"],
            "qualification": "Legacy cache dates are not independently bound to native index bytes"}


def summarize(manifest_path, supplier_receipt_path, output):
    manifest_path, supplier_receipt_path = Path(manifest_path), Path(supplier_receipt_path)
    checked = verify_pilot(manifest_path, with_verification=True)
    manifest = checked["manifest"]
    run = manifest_path.parent
    supplier = json.loads(supplier_receipt_path.read_text(encoding="utf-8"))
    if supplier.get("schema") != "FIVE_YEAR_SUPPLIER_PROBE_V1" or supplier.get("formal_admission") is not False:
        raise ValueError("explicit exploratory supplier receipt required")
    if supplier.get("sample_sha256") != manifest["sample_sha256"] or supplier.get("synthetic_only") is not False:
        raise ValueError("supplier probe must bind the same real frozen sample")
    for attempt in supplier["attempts"]:
        if "native_file" in attempt:
            path = safe_path(supplier_receipt_path.parent / attempt["native_file"], supplier_receipt_path.parent)
            if sha256_file(path) != attempt["response_sha256"] or path.stat().st_size != attempt["response_bytes"]:
                raise ValueError("supplier native source bytes do not match receipt")
    bars = pd.read_parquet(run / "bars.parquet")
    calendar = pd.read_csv(run / "calendar.csv", parse_dates=["date"])["date"]
    sample = json.loads((run / "sample.json").read_text(encoding="utf-8"))
    monthly = coverage_table(bars, calendar, sample["symbols"], manifest["research_start"], manifest["research_end"])
    output = safe_path(Path(output))
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "coverage-monthly.csv", monthly.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    annual = figures(monthly, output)
    write_new(output / "missing-annual.csv", annual.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    native_index = []
    for path in sorted((run / "evidence" / "sources").rglob("*.request.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        native_index.append({key: item.get(key) for key in (
            "interface", "interface_arguments", "url", "status_code", "requested_at", "retrieved_at",
            "response_sha256", "content_type", "upstream_vintage")})
    coverage = json.loads((run / "coverage.json").read_text(encoding="utf-8"))
    summary = {"schema": "FIVE_YEAR_PUBLIC_DERIVED_AUDIT_V2", "purpose": "EXPLORATORY",
               "formal_admission": False, "synthetic_only": False, "no_live_money": True,
               "created_at": datetime.now(timezone.utc).isoformat(), "data_version": manifest["data_version"],
               "manifest_sha256": sha256_file(manifest_path), "receipt_sha256": sha256_file(run / "receipt.json"),
               "supplier_receipt_sha256": sha256_file(supplier_receipt_path),
               "source_version": manifest["source_version"], "implementation_sha256": manifest["implementation_sha256"],
               "sample_sha256": manifest["sample_sha256"], "sample_frozen_at": sample["frozen_at"],
               "statuses": manifest["statuses"], "metrics": manifest["metrics"],
               "research_start": manifest["research_start"], "research_end": manifest["research_end"],
               "warmup": manifest["warmup"], "terminal_labels": manifest["terminal_labels"], "coverage": coverage,
               "native_response_index": native_index,
               "benchmark_provenance": verified_benchmark_provenance(checked),
               "supplier_coverage_replay": replay_supplier_coverage(supplier, supplier_receipt_path.parent, calendar),
               "supplier_original_attempts": len(supplier["attempts"]), "supplier_http_attempts": supplier["http_attempts"],
               "privacy": "Derived coverage, request contracts and SHA only; no prices, quantities, returns, factor values or vendor response bodies",
               "revision_and_vintage": "UNKNOWN_UNVERIFIED; acquisition date is not historical availability"}
    # Supplier native_file is an audit-relative pointer, not content. No local absolute paths.
    write_new(output / "summary.json", encoded(summary))
    files = {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size}
             for p in sorted(output.iterdir()) if p.is_file()}
    write_new(output / "publication-receipt.json", encoded({
        "schema": "FIVE_YEAR_DERIVED_PUBLICATION_RECEIPT_V1", "formal_admission": False,
        "created_at": datetime.now(timezone.utc).isoformat(), "files": files,
        "native_data_published": False, "data_version": manifest["data_version"]}))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--supplier-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.manifest, args.supplier_receipt, args.output)
    print(json.dumps({"data_version": result["data_version"], "statuses": result["statuses"]}))


if __name__ == "__main__":
    main()
