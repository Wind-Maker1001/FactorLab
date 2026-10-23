"""Aggregate-only supplement: existing 50 names, units, GBBQ evidence and plot."""

import argparse
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(private, audit, output):
    if (private / "results/diagnostics/tdx-data-quality-audit").resolve() not in output.resolve().parents:
        raise ValueError("supplement output must stay in private diagnostics")
    output.mkdir(parents=True, exist_ok=False)
    inventory = pd.read_csv(audit / "security-inventory-private.csv")
    prior = private / "data/research/exploratory/a-share-50-20261014-v1/bars.parquet"
    old = pd.read_parquet(prior)
    names = sorted(old.symbol.unique())
    matched = inventory[inventory.symbol.isin(names)]
    missing = sorted(set(names) - set(matched.symbol))
    fifty = {"schema": "EXISTING_FROZEN_50_TDX_COVERAGE_V1", "source_sha256": sha(prior),
             "selection": "unchanged existing 50-name experiment; no reselection", "names": len(names),
             "existing_rows": len(old), "existing_start": str(old.date.min().date()),
             "existing_end": str(old.date.max().date()), "tdx_present": len(matched),
             "tdx_absent": len(missing), "tdx_full_history_files": int(matched.history_complete.sum()),
             "tdx_observed_history_grid": int(matched.history_observed.sum()),
             "tdx_expected_history_grid": len(names) * 1341, "missing_reason": "UNKNOWN",
             "complete_five_year_hfq_four_prices": "UNVERIFIED", "historical_turnover": "UNVERIFIED",
             "historical_identity_and_status": "UNVERIFIED", "pit": False, "financial_admission": False}
    conversions = pd.read_csv(private / "results/diagnostics/tdx-local-data/20261011-tdx-raw-conversion-v1/conversion_status.csv")
    pilot = private / "data/research/five-year-pilot/20261015-native-calendar-v1/runs/20261015T111701027971Z-d51bb4d1"
    bars = pd.read_parquet(pilot / "bars.parquet")
    units = []
    for symbol in json.loads((pilot / "sample.json").read_bytes())["symbols"]:
        entry = conversions[conversions.symbol.eq(symbol)]
        if entry.empty:
            units.append({"symbol": symbol, "pairs": 0, "status": "NO_VALID_REFERENCE"})
            continue
        tdx = pd.read_parquet(Path(entry.iloc[0].parquet_file)).set_index("date")
        reference = bars[bars.symbol.eq(symbol)].set_index("date")[["volume", "amount"]].dropna()
        joined = tdx[["volume", "amount"]].join(reference, how="inner", lsuffix="_tdx", rsuffix="_em")
        units.append({"symbol": symbol, "pairs": len(joined),
                      "tdx_volume_equals_em_shares": int((joined.volume_tdx == joined.volume_em).sum()),
                      "tdx_volume_times100_equals_em_shares": int((joined.volume_tdx.astype(float) * 100 == joined.volume_em).sum()),
                      "tdx_amount_equals_em_cny": int((joined.amount_tdx == joined.amount_em).sum()),
                      "tdx_amount_equals_float32_em_cny": int((joined.amount_tdx == joined.amount_em.to_numpy().astype("float32").astype(float)).sum()),
                      "status": "SOURCE_UNIT_UNVERIFIED"})
    unit_result = {"schema": "TDX_UNIT_CANDIDATES_V1", "raw_types": {"price": "little-endian uint32", "volume": "little-endian uint32", "amount": "little-endian float32"},
                   "candidate_volume_units": ["shares", "lots of 100 shares"], "candidate_amount_unit": "CNY",
                   "unit_certification": "UNKNOWN", "zero_tolerance": True,
                   "float32_comparison": "storage-representation diagnostic only; not tolerance widening",
                   "samples": units}
    official_root = private / "results/diagnostics/real-hfq-pilot-delivery/20261014-v1/classified-view-v3"
    registry = json.loads((official_root / "source-registry-final.json").read_bytes())
    notice = next(r for r in registry["sources"] if r["id"] == "OFFICIAL_NOTICE")
    official = json.loads((official_root / "official-review.json").read_bytes())
    assert sha(official_root / notice["path"]) == notice["sha256"] == official["source_sha256"]
    assert official["ex_date"] == "2025-07-16" and official["cash_cny_per_old_share"] == "0.41"
    legacy_gbbq = json.loads((official_root / "gbbq-review.json").read_bytes())
    new_gbbq = json.loads((audit / "gbbq-summary.json").read_bytes())
    for item in legacy_gbbq["checks"]:
        assert sha(private / item["legacy_archive"]) == item["expected_sha256"]
        assert new_gbbq["inputs"][Path(item["source_path"]).name]["sha256"] == item["expected_sha256"]
    hq = Path(legacy_gbbq["checks"][0]["source_path"]).parent
    lines = [line for line in (hq / "gbbq.map").read_bytes().splitlines() if line.strip()]
    events = pd.read_parquet(audit / "gbbq-candidates-private.parquet")
    groups = events[events.duplicated(["market", "code", "date", "category"], keep=False)]
    supplement = {"schema": "GBBQ_SINGLE_EVENT_EVIDENCE_BINDING_V1", "old_gbbq_archives_still_match": True,
                  "official_notice_sha256": notice["sha256"], "official_review_sha256": sha(official_root / "official-review.json"),
                  "event": {"symbol": "600000.SH", "ex_date": official["ex_date"], "cash_cny_per_old_share": official["cash_cny_per_old_share"]},
                  "event_chain_complete": "UNKNOWN", "hfq": "HFQ_UNVERIFIED",
                  "map_nonempty_lines": len(lines), "map_line_lengths": sorted(set(map(len, lines))),
                  "map_ascii_fixed_numeric_layout": all(re.fullmatch(rb"[0-9]{10}  ", line) for line in lines),
                  "map_field_semantics": "UNKNOWN; not asserted to be record offsets, dates or identity authority",
                  "duplicate_key_groups": int(groups.groupby(["market", "code", "date", "category"]).ngroups),
                  "duplicate_key_is_correction_evidence": False,
                  "candidate_records_after_research_end": int((events.date > 20260911).sum()),
                  "candidate_records_after_client_audit_date": int((events.date > 20261016).sum()),
                  "client_audit_date": "2026-10-16",
                  "revision_metadata_fields_in_candidate_layout": 0}
    for filename, value in (("existing-50-summary.json", fifty), ("units-summary.json", unit_result), ("gbbq-evidence-binding.json", supplement)):
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    plot(pd.read_csv(audit / "coverage-monthly.csv"), output / "coverage.png")
    receipt = {"schema": "TDX_AGGREGATE_SUPPLEMENT_RECEIPT_V1", "financial_admission": False,
               "script_sha256": sha(Path(__file__)), "audit_summary_sha256": sha(audit / "summary.json"),
               "files": {p.name: {"sha256": sha(p), "bytes": p.stat().st_size} for p in sorted(output.iterdir())}}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"existing_50": fifty, "units": units, "gbbq_evidence": supplement}, ensure_ascii=False, indent=2))


def plot(coverage, target):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    months = sorted(coverage.month.unique())
    fig, axes = plt.subplots(2, 1, figsize=(14, 5.4), sharex=True)
    for ax, only_a in zip(axes, (False, True)):
        selected = coverage[coverage.security_type_candidate.eq("A_SHARE_CANDIDATE")] if only_a else coverage
        group = selected.groupby(["market_directory_candidate", "month"])[["observed_grid", "expected_grid"]].sum()
        values = np.array([[group.loc[(m, month), "observed_grid"] / group.loc[(m, month), "expected_grid"]
                            if (m, month) in group.index else np.nan for month in months] for m in ("sh", "sz", "bj")])
        image = ax.imshow(values, vmin=0, vmax=1, cmap="Blues", aspect="auto")
        ax.set_yticks(range(3), ["SH directory", "SZ directory", "BJ directory"])
        ax.set_title("A-share prefix candidates only" if only_a else "All instrument candidates", loc="left", fontsize=12)
    ticks = [i for i, m in enumerate(months) if m.endswith("-01") or i == 0 or i == len(months) - 1]
    axes[-1].set_xticks(ticks, [months[i] for i in ticks])
    fig.subplots_adjust(left=.11, right=.88, top=.84, bottom=.2, hspace=.5)
    cax = fig.add_axes([.91, .25, .015, .52])
    bar = fig.colorbar(image, cax=cax)
    bar.set_ticks([0, .25, .5, .75, 1])
    bar.set_ticklabels(["0%", "25%", "50%", "75%", "100%"])
    fig.suptitle("Local TDX coverage against observed native benchmark sessions", fontsize=15, x=.11, ha="left")
    fig.text(.11, .09, "2021-03-08 to 2026-09-11 | observed security-session grid / expected grid for every local candidate file", fontsize=10)
    fig.text(.11, .045, "Missing reason UNKNOWN. Directory markets and identities unverified. This is not CSI500/PIT member coverage.", fontsize=10)
    fig.savefig(target, dpi=140, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("private-root", "audit", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    run(args.private_root, args.audit, args.output)
