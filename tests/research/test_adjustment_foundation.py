"""Independent synthetic Golden and whole-Builder information-boundary tests."""

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qbot.research.data.adjustment import build_hfq, validate_adjustment
from qbot.research.data.adjustment.contracts import AFFINE, RATIO, SourceAdjustmentContract
from qbot.research.data.corporate_actions import select_events, normalize_per_share
from qbot.research.data.corporate_actions.store import load_snapshot, publish_snapshot
from qbot.research.data.evidence import table_sha256, timestamp
from qbot.research.errors import DataQualityError
from tests.research.market_data_fixture import anchor, build, cutoffs, event, evidence, raw, snapshot


GOLDEN = Path(__file__).parent / "fixtures/adjustment_golden"
CASES = json.loads((GOLDEN / "scenarios.json").read_text(encoding="utf-8"))["cases"]


def test_independent_golden_bytes_are_frozen():
    m = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))
    assert m["synthetic_only"] and not m["research_conclusions_permitted"]
    assert m["atol"] == m["rtol"] == 1e-12
    for name, digest in m["sha256"].items():
        assert hashlib.sha256((GOLDEN / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
@pytest.mark.parametrize("method", [AFFINE, RATIO])
def test_prices_match_independent_rational_golden(tmp_path, case, method):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=4)
    source = raw(dates, [100, 100, float(case["post_open"]), float(case["post_open"])])
    terms = {name: Decimal(case[name]) for name in ("cash", "bonus", "capitalization", "rights")}
    if "rights_price" in case:
        terms.update(rights_price=Decimal(case["rights_price"]), subscription_start="2023-12-01",
                     subscription_end="2024-01-03", eligibility="synthetic eligible old-share owners")
    if "split_ratio" in case:
        terms["split_ratio"] = Decimal(case["split_ratio"])
    action = event(artifact, event_type=case["kind"], **terms)
    result = build(source, snapshot(artifact, (action,)), artifact, method)
    prefix = "affine" if method == AFFINE else "ratio"
    multiplier = float(Fraction(case[prefix + "_multiplier"]))
    offset = float(case["affine_offset"]) if method == AFFINE else 0
    assert result.coefficients.multiplier.iloc[2] == pytest.approx(multiplier, abs=1e-12, rel=1e-12)
    assert result.coefficients.offset.iloc[2] == offset
    assert result.bars.adj_open.iloc[2] == pytest.approx(float(Fraction(case[prefix+"_open"])), abs=1e-12, rel=1e-12)
    reference = source[["date", "symbol"]].copy()
    for field in ("open", "high", "low", "close"):
        expected = source[field].copy()
        expected.iloc[2:] = expected.iloc[2:] * multiplier + offset
        reference["adj_" + field] = expected
    contract = SourceAdjustmentContract("INDEPENDENT_RATIONAL_ORACLE", "golden-v1", method,
                                        artifact.sha256, table_sha256(reference), synthetic_only=True)
    report = validate_adjustment(result, reference, contract)
    assert report.status == "PASS" and report.synthetic_only
    assert result.evidence["price_representation"] == "HFQ_NOT_INVESTOR_TOTAL_RETURN"
    assert np.all(result.bars.adj_high >= result.bars[["adj_open", "adj_close"]].max(axis=1))


@pytest.mark.parametrize("method", [AFFINE, RATIO])
def test_multiple_actions_and_reused_anchor_stitch(tmp_path, method):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=7)
    source = raw(dates, [100, 100, 98, 98, 95, 95, 95])
    actions = (event(artifact), event(artifact, day="2024-01-08", event_id="cash-2",
                                      cash=Decimal(3), reference_close=Decimal(98)))
    original = build(source, snapshot(artifact, actions), artifact, method)
    assert original.bars.adj_open.iloc[-1] == pytest.approx(100, abs=1e-12)
    offset = Decimal(2) if method == AFFINE else Decimal(0)
    multiplier = Decimal(1) if method == AFFINE else Decimal(50) / Decimal(49)
    new_anchor = replace(anchor(artifact, method), date="2024-01-04", multiplier=multiplier, offset=offset)
    latter = build(source.iloc[2:].reset_index(drop=True), snapshot(artifact, actions), artifact,
                    method, new_anchor)
    pd.testing.assert_frame_equal(latter.bars, original.bars.iloc[2:].reset_index(drop=True), check_exact=True)


@pytest.mark.parametrize("field,value,match", [
    ("cash", None, "explicit decimal"), ("rights_price", None, "rights_price"),
    ("subscription_end", None, "subscription"), ("eligibility", None, "eligibility"),
    ("reference_close", None, "reference_close"), ("rule_id", "UNKNOWN", "UNSUPPORTED"),
    ("record_date", None, "record date"),
])
def test_incomplete_terms_fail_closed(tmp_path, field, value, match):
    artifact = evidence(tmp_path)
    action = event(artifact, event_type="RIGHTS", cash=Decimal(0), rights=Decimal("0.5"), rights_price=Decimal(20),
                   subscription_start="2023-12-01", subscription_end="2024-01-03", eligibility="old holders")
    action = replace(action, **{field: value})
    with pytest.raises(DataQualityError, match=match):
        build(raw(pd.bdate_range("2024-01-02", periods=4)), snapshot(artifact, (action,)), artifact)


@pytest.mark.parametrize("kind", ["MERGER", "DELISTING", "OTHER", "CODE_CHANGE"])
def test_special_actions_are_not_simplified(tmp_path, kind):
    artifact = evidence(tmp_path)
    with pytest.raises(DataQualityError, match="UNSUPPORTED"):
        build(raw(pd.bdate_range("2024-01-02", periods=4)), snapshot(artifact, (event(artifact, event_type=kind),)), artifact)


def test_empty_response_and_late_unknown_action_never_become_identity_factor(tmp_path):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=4)
    incomplete = replace(snapshot(artifact), coverage=())
    with pytest.raises(DataQualityError, match="INCOMPLETE event coverage"):
        build(raw(dates), incomplete, artifact)
    action = event(artifact, available_at="2024-01-05", announced_at="2024-01-03")
    with pytest.raises(DataQualityError, match="EVENT_INFORMATION_UNAVAILABLE"):
        build(raw(dates), snapshot(artifact, (action,)), artifact)


def test_date_only_announcement_uses_end_of_day_not_morning(tmp_path):
    assert timestamp("2024-01-04") > timestamp("2024-01-04T15:30:00+08:00")
    artifact = evidence(tmp_path)
    action = event(artifact, available_at="2024-01-04", announced_at="2024-01-04")
    with pytest.raises(DataQualityError, match="UNAVAILABLE"):
        build(raw(pd.bdate_range("2024-01-02", periods=4)), snapshot(artifact, (action,)), artifact)


def test_old_revision_survives_future_correction(tmp_path):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=8)
    source = raw(dates, [100, 100, 98, 98, 98, 98, 98, 98])
    initial = event(artifact)
    corrected = replace(initial, revision=2, cash=Decimal(3), announced_at="2024-01-10", available_at="2024-01-10")
    before = build(source, snapshot(artifact, (initial,)), artifact)
    after = build(source, snapshot(artifact, (initial, corrected)), artifact)
    prefix = source.date <= pd.Timestamp("2024-01-10")
    pd.testing.assert_frame_equal(before.bars.loc[prefix], after.bars.loc[prefix], check_exact=True)
    assert "LATE_EVENT_REVISION_REQUIRES_VINTAGE_FEATURE_VIEW" in after.issues
    assert after.bars.adj_open.iloc[-1] == 101


@pytest.mark.parametrize("change", ["cancel", "reschedule_outside_window"])
def test_late_cancel_or_reschedule_keeps_past_but_rejects_stitched_vintage(tmp_path, change):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=8)
    source = raw(dates, [100, 100, 98, 98, 98, 98, 98, 98])
    initial = event(artifact)
    revision = replace(initial, revision=2, announced_at="2024-01-10", available_at="2024-01-10")
    revision = replace(revision, status="CANCELLED") if change == "cancel" else replace(revision, effective_date="2024-02-01")
    before = build(source, snapshot(artifact, (initial,)), artifact)
    after = build(source, snapshot(artifact, (initial, revision)), artifact)
    mask = source.date.le(pd.Timestamp("2024-01-10"))
    pd.testing.assert_frame_equal(before.bars.loc[mask], after.bars.loc[mask], check_exact=True)
    assert after.bars.adj_open.iloc[-1] == 98
    assert "LATE_EVENT_REVISION_REQUIRES_VINTAGE_FEATURE_VIEW" in after.issues


def test_historical_reconstruction_cannot_pass_strict_reference_qualification(tmp_path):
    artifact = evidence(tmp_path)
    source = raw(pd.bdate_range("2024-01-02", periods=3))
    result = build(source, replace(snapshot(artifact), history_kind="HISTORICALLY_RECONSTRUCTED"), artifact)
    contract = SourceAdjustmentContract("INDEPENDENT_ORACLE", "v1", AFFINE, artifact.sha256,
                                        table_sha256(result.bars), synthetic_only=True)
    assert validate_adjustment(result, result.bars.copy(), contract).status == "INCOMPLETE"


def test_same_day_event_bases_must_be_explicit(tmp_path):
    artifact = evidence(tmp_path)
    first = event(artifact, sequence=1)
    second = event(artifact, event_id="second", sequence=2, cash=Decimal(3), reference_close=Decimal(98),
                    basis="SEQUENTIAL_AFTER_PREVIOUS_EVENT")
    source = raw(pd.bdate_range("2024-01-02", periods=4), [100, 100, 95, 95])
    result = build(source, snapshot(artifact, (first, second)), artifact)
    assert result.bars.adj_open.iloc[-1] == 100
    for malformed in (replace(second, sequence=1), replace(second, basis="PER_OLD_SHARE")):
        with pytest.raises(DataQualityError, match="same-day"):
            build(source, snapshot(artifact, (first, malformed)), artifact)


def test_suspension_missing_prices_remain_missing_and_event_applies_on_effective_date(tmp_path):
    artifact = evidence(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=6)
    source = raw(dates, [100, 100, np.nan, np.nan, 98, 98])
    result = build(source, snapshot(artifact, (event(artifact),)), artifact)
    assert result.bars.loc[2:3, ["adj_open", "adj_high", "adj_low", "adj_close"]].isna().all().all()
    assert result.coefficients.offset.iloc[2] == 2
    assert result.bars.adj_open.iloc[-1] == 100


def test_event_store_idempotence_and_source_tamper_rejection(tmp_path):
    artifact = evidence(tmp_path)
    source = snapshot(artifact, (event(artifact),))
    folder = publish_snapshot(source, tmp_path / "events")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    assert publish_snapshot(source, tmp_path / "events") == folder
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()} == hashes
    assert load_snapshot(folder).events == source.events
    (folder / (artifact.sha256 + ".bin")).write_bytes(b"tamper")
    with pytest.raises(DataQualityError, match="hash"):
        load_snapshot(folder)


def test_per_share_conversion_requires_explicit_units():
    assert normalize_per_share("4.1", "10") == Decimal("0.41")
    for base in (None, "0", "UNKNOWN"):
        with pytest.raises(DataQualityError):
            normalize_per_share("4.1", base)


@pytest.mark.parametrize("method", [AFFINE, RATIO])
@pytest.mark.parametrize("change", ["future_event", "late_correction", "late_cancel", "late_reschedule"])
def test_future_prices_and_events_leave_builder_features_and_fixed_signal_unchanged(tmp_path, method, change):
    from qbot.research.config import load_config
    from qbot.research.dataset import DatasetBuilder, DatasetBuildOptions
    from qbot.research.models.factor_score import FactorScoreModel
    from qbot.research.universe.filters import UniverseFilterConfig
    artifact = evidence(tmp_path)
    config = load_config(Path(__file__).resolve().parents[2] / "configs/research/csi500_5y.yaml")
    dates = pd.bdate_range("2024-01-02", periods=150)
    base = raw(dates, 100 + np.sin(np.arange(len(dates)) / 4))
    action_position = 110 if change == "future_event" else 90
    action_day = dates[action_position].date().isoformat()
    action = event(artifact, day=action_day,
                   reference_close=Decimal(str(base.close.iloc[action_position - 1])))
    first_snapshot = snapshot(artifact, (action,))
    changed = base.copy()
    cutoff = dates[100]
    for field in ("open", "high", "low", "close", "amount"):
        changed.loc[changed.date.gt(cutoff), field] *= 1000000
    changed_action = replace(action, cash=Decimal(5),
                             announced_at=str(dates[103].date()), available_at=str(dates[103].date()))
    if change == "future_event":
        changed_action = replace(changed_action, reference_close=Decimal(str(changed.close.iloc[109])))
        changed_events = (changed_action,)
    else:
        changed_action = replace(changed_action, revision=2)
        if change == "late_cancel":
            changed_action = replace(changed_action, status="CANCELLED")
        elif change == "late_reschedule":
            changed_action = replace(changed_action, effective_date="2024-12-20")
        changed_events = (action, changed_action)
    results = [build(base, first_snapshot, artifact, method),
               build(changed, snapshot(artifact, changed_events), artifact, method)]
    if change != "future_event":
        assert "LATE_EVENT_REVISION_REQUIRES_VINTAGE_FEATURE_VIEW" in results[1].issues
        contract = SourceAdjustmentContract("INDEPENDENT_ORACLE", "v1", method, artifact.sha256,
                                            table_sha256(results[1].bars), synthetic_only=True)
        assert validate_adjustment(results[1], results[1].bars.copy(), contract).status == "INCOMPLETE"

    def dataset(prices, adjustment):
        stocks = prices.copy()
        for field in ("open", "high", "low", "close"):
            stocks["raw_" + field] = stocks[field]
            stocks["adj_" + field] = adjustment.bars["adj_" + field]
        stocks["adj_factor"] = stocks.adj_close / stocks.raw_close
        stocks["turnover"] = 0.01
        other = stocks.copy()
        other["symbol"] = "000001.SZ"
        for field in ("open", "high", "low", "close"):
            other[field] *= 1.1
            other["raw_"+field] *= 1.1
            other["adj_"+field] *= 1.1
        benchmark = stocks.copy()
        benchmark["symbol"] = "000905.SH"
        for field in ("open", "high", "low", "close"):
            benchmark["adj_"+field] = base[field].to_numpy()
            benchmark[field] = base[field].to_numpy()
            benchmark["raw_"+field] = base[field].to_numpy()
        benchmark["adj_factor"] = 1.0
        membership = pd.MultiIndex.from_product([dates[70:], ["600000.SH", "000001.SZ"]], names=["date", "symbol"]).to_frame(index=False)
        membership["is_member"] = True
        status = membership[["date", "symbol"]].assign(trade_status=1, is_st=False)
        listings = pd.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "listing_date": [dates[0], dates[0]]})
        options = DatasetBuildOptions(config.factors, expected_member_count=2, minimum_cross_section=2,
                                      filter_config=UniverseFilterConfig(min_listed_days=60))
        return DatasetBuilder(options).build(pd.concat([stocks, other], ignore_index=True), benchmark,
                                              membership, status, listings, dates)

    before, after = dataset(base, results[0]), dataset(changed, results[1])
    columns = ["date", "symbol"] + list(config.factors) + ["raw_"+n for n in config.factors] + ["MARKET_REGIME", "INDEX_RET5", "INDEX_RET20", "INDEX_RET60", "INDEX_VOL20"]
    left = before.loc[before.date.le(cutoff), columns].reset_index(drop=True)
    right = after.loc[after.date.le(cutoff), columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right, check_exact=True)
    model = FactorScoreModel(config.models.factor_score.weights)
    model.fit(left[list(config.factors)])
    pd.testing.assert_series_equal(model.predict(left[list(config.factors)]), model.predict(right[list(config.factors)]), check_exact=True)
    # Evaluation deliberately sees future facts; they never become feature columns.
    a = before.loc[before.date.eq(dates[96]), "EXCESS_RET5"]
    b = after.loc[after.date.eq(dates[96]), "EXCESS_RET5"]
    assert not np.allclose(a, b)
