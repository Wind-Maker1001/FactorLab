"""Verify the shipped real-data derivative audit, without private market bytes."""

from pathlib import Path

import pandas as pd
from PIL import Image

from qbot.research.exploratory.statistical_audit.delivery import (
    MARKERS,
    PUBLIC_FILES,
    verify_audit,
)
from qbot.research.exploratory.statistical_audit.inputs import DATA_VERSION
from qbot.research.exploratory.runner import FACTOR_NAMES


EVIDENCE = (
    Path(__file__).resolve().parents[2]
    / "docs/research/evidence/exploratory-statistical-audit-v3"
)


def test_complete_real_audit_receipt_and_fixed_families_survive_checkout():
    r = verify_audit(EVIDENCE / "receipt.json", public=True)
    assert r["status"] == "SUCCESS" and r["data_version"] == DATA_VERSION
    assert set(r["artifacts"]) == set(PUBLIC_FILES)
    mean = pd.read_csv(EVIDENCE / "rankic_inference.csv")
    state = pd.read_csv(EVIDENCE / "regime_comparisons.csv")
    assert len(mean) == 14 * 3 and len(state) == 14 * 3 * 3
    assert set(mean.factor) == set(FACTOR_NAMES)
    assert mean.groupby("method").size().eq(14).all()
    assert state.groupby("method").size().eq(42).all()
    assert mean.valid_dates.eq(236).all()
    assert not state.loc[
        state.comparison.str.contains("SIDEWAYS"), "conditional_support"
    ].any()
    assert state.loc[
        state.comparison.str.contains("SIDEWAYS"), "sample_insufficient"
    ].all()


def test_all_quintile_days_and_unmatured_tail_are_explicit_without_private_ids():
    q = pd.read_csv(EVIDENCE / "quintile_daily.csv")
    assert len(q) == 14 * 242 * 5
    assert not q.duplicated(["factor", "date", "quantile"]).any()
    assert "symbol" not in q and "EXCESS_RET5" not in q
    assert not any("sha256" in column or "hash" in column for column in q)
    tail = q.loc[q.date.ge("2026-09-04")]
    assert len(tail) == 14 * 6 * 5
    assert not tail.A_formed.any() and tail.B_formed.all()
    assert tail.B_mean.isna().all() and tail.A_mean.isna().all()
    assert tail.B_observable_members.eq(0).all()
    assert not (EVIDENCE / "private_quintile_membership.csv").exists()


def test_chinese_report_and_four_figures_have_all_six_disclosures():
    report = (EVIDENCE / "report.md").read_text(encoding="utf-8")
    assert all(marker in report for marker in MARKERS)
    assert "SIDEWAYS45" in report and "五年" in report
    figures = list(EVIDENCE.glob("*.png"))
    assert len(figures) == 4
    for path in figures:
        with Image.open(path) as figure:
            assert figure.width >= 1760 and figure.height >= 1280
            assert all(
                marker in figure.info.get("Description", "") for marker in MARKERS
            )
