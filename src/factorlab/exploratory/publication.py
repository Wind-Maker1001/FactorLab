"""Publish only reviewed derived exploration artifacts, never market bytes."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from .contract import (
    ExploratoryContractError,
    REQUIRED_MARKERS,
    assert_exploratory_only,
    sha256_file,
)
from .runner import verify_receipt


DERIVED_FILES = frozenset(
    {
        "summary.md",
        "daily_ic.csv",
        "factor_report.csv",
        "quantile_returns.csv",
        "factor_autocorrelation.csv",
        "factor_turnover.csv",
        "factor_correlation.csv",
        "daily_coverage.csv",
        "factor_quality.csv",
        "regime_counts.csv",
        "security_coverage.csv",
        "data_quality.json",
        "frozen_sample.json",
        "factor_ic.png",
        "rank_ic.png",
        "factor_correlation.png",
        "quantile_returns.png",
        "regime_performance.png",
        "daily_coverage.png",
        "daily_ic.png",
        "factor_completeness.png",
        "temporal_diagnostics.png",
        "data_gaps.png",
    }
)


def _identity(value):
    payload = {
        key: content for key, content in value.items() if key != "receipt_sha256"
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def publish_derived(run_directory, destination):
    """Create a new derivative-only package from a verified private run."""
    run_directory, destination = Path(run_directory), Path(destination)
    run = verify_receipt(run_directory / "receipt.json")
    assert_exploratory_only(run)
    if run.get("synthetic_only") is not False:
        raise ExploratoryContractError(
            "synthetic or unclassified run cannot supply real publication"
        )
    if destination.exists():
        raise ExploratoryContractError(
            "publication is immutable; use a new destination"
        )
    destination.mkdir(parents=True)
    for name in sorted(DERIVED_FILES.intersection(run["artifacts"])):
        shutil.copyfile(run_directory / name, destination / name)
    # The report's provenance link resolves to a source index with hashes only.
    # Source/SDK/HTTP/DAY/price/stock-level label bytes remain private.
    index = {
        "schema": "EXPLORATORY_PUBLIC_SOURCE_INDEX_V1",
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "formal_admission": False,
        "markers": list(REQUIRED_MARKERS),
        "data_version": run.get("data_version"),
        "source_version": run.get("source_version"),
        "data_files": run.get("data_files", {}),
        "raw_source_bytes_published": False,
        "reproduction_requires_authorized_private_input": True,
    }
    manifest = run_directory / "input_manifest.json"
    if manifest.exists():
        private = json.loads(manifest.read_text(encoding="utf-8"))
        for key in (
            "vendor",
            "retrieved_at",
            "research_start",
            "research_end",
            "warmup_min_sessions",
            "units",
            "price_basis",
            "benchmark_basis",
            "interfaces",
            "coverage",
            "limitations",
            "source_contract",
        ):
            if key in private:
                index[key] = private[key]
        index["source_artifacts"] = [
            {key: item[key] for key in ("sha256", "source_version") if key in item}
            for item in private.get("sources", [])
        ]
        index["excluded_securities"] = [
            {
                "symbol": item.get("symbol"),
                "failure_type": str(item.get("reason", "UNKNOWN")).split(":", 1)[0],
            }
            for item in private.get("excluded_securities", [])
        ]
    (destination / "input_manifest.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    receipt = {
        "schema": "EXPLORATORY_PUBLICATION_V1",
        "status": run["status"],
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "formal_admission": False,
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "markers": list(REQUIRED_MARKERS),
        "forward_allowed": False,
        "live_allowed": False,
        "synthetic_only": False,
        "data_version": run.get("data_version"),
        "source_version": run.get("source_version"),
        "private_run_receipt_sha256": sha256_file(run_directory / "receipt.json"),
        "code_and_environment": run["code_and_environment"],
        "contract": run["contract"],
        "reasons": run["reasons"],
        "raw_source_bytes_published": False,
        "artifacts": {
            path.name: sha256_file(path)
            for path in sorted(destination.iterdir())
            if path.is_file()
        },
    }
    receipt["receipt_sha256"] = _identity(receipt)
    (destination / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    verify_publication(destination / "receipt.json")
    return destination


def verify_publication(path):
    path = Path(path)
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert_exploratory_only(receipt)
    if receipt.get("schema") != "EXPLORATORY_PUBLICATION_V1" or receipt.get(
        "receipt_sha256"
    ) != _identity(receipt):
        raise ExploratoryContractError(
            "publication receipt schema or semantic hash mismatch"
        )
    if receipt.get("status") not in {"SUCCESS", "BLOCKED"}:
        raise ExploratoryContractError("unsupported publication status")
    if (
        receipt.get("synthetic_only") is not False
        or receipt.get("raw_source_bytes_published") is not False
    ):
        raise ExploratoryContractError("unsafe publication boundary")
    if (
        receipt.get("forward_allowed") is not False
        or receipt.get("live_allowed") is not False
    ):
        raise ExploratoryContractError(
            "publication cannot authorize Forward or live inputs"
        )
    allowed = DERIVED_FILES | {"input_manifest.json"}
    if not set(receipt["artifacts"]).issubset(allowed):
        raise ExploratoryContractError("publication includes non-allowlisted artifacts")
    actual = {
        entry.name for entry in path.parent.iterdir() if entry.name != "receipt.json"
    }
    if actual != set(receipt["artifacts"]):
        raise ExploratoryContractError("publication artifact set mismatch")
    for name, digest in receipt["artifacts"].items():
        artifact = path.parent / name
        if (
            not artifact.is_file()
            or artifact.is_symlink()
            or sha256_file(artifact) != digest
        ):
            raise ExploratoryContractError(
                "publication artifact hash mismatch: %s" % name
            )
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Publish derived EXPLORATORY artifacts; NO LIVE MONEY"
    )
    parser.add_argument("--run", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if args.verify:
        receipt = verify_publication(args.verify)
        print(json.dumps({"verified": True, "status": receipt["status"]}))
    elif args.run and args.destination:
        print(str(publish_derived(args.run, args.destination)))
    else:
        parser.error("provide --verify or both --run and --destination")


if __name__ == "__main__":
    main()
