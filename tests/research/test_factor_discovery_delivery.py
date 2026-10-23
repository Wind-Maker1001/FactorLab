"""The committed public reports remain independently inspectable and bounded."""

import json
from pathlib import Path

import pandas as pd
import pytest

from qbot.research.exploratory.factor_discovery.publication import (
    PUBLIC_FILES,
    PUBLIC_COLUMNS,
)
from qbot.research.exploratory.factor_discovery.receipts import verify_receipt


@pytest.mark.parametrize("mode", ["synthetic", "private"])
def test_committed_public_package_receipt_expressions_and_aggregate_schema(mode):
    root = Path(__file__).resolve().parents[2]
    directory = root / "docs/research/evidence" / ("factor-discovery-%s-v2" % mode)
    receipt = verify_receipt(directory / "receipt.json")
    assert set(path.name for path in directory.iterdir()) == PUBLIC_FILES | {
        "receipt.json"
    }
    assert receipt["status"] == "SOFTWARE_MVP_SUCCESS"
    assert receipt["counts"]["successful"] == 39
    assert (
        receipt["formal_admission"]
        is receipt["forward_allowed"]
        is receipt["live_allowed"]
        is False
    )
    assert receipt["input"]["synthetic_only"] is (mode == "synthetic")
    for name, columns in PUBLIC_COLUMNS.items():
        assert set(pd.read_csv(directory / name, nrows=0).columns) == columns
    candidates = pd.read_csv(directory / "candidates.csv")
    assert len(candidates) == 40
    assert (
        candidates.loc[
            candidates["status"].eq("SUCCESS"), "expression_sha256"
        ].nunique()
        == 39
    )
    assert (
        candidates["dimension"]
        .map(json.loads)
        .map(lambda value: value["is_dimensionless"])
        .all()
    )
    quantiles = pd.read_csv(directory / "quantile_returns.csv")
    assert set(quantiles["quantile"]) == {"Q1", "Q2", "Q3", "Q4", "Q5", "Q5-Q1"}
    assert "NO LIVE MONEY" in (directory / "summary.md").read_text(encoding="utf-8")
