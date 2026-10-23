"""Independent numeric comparison is scoped evidence, never promotion."""

import json
from pathlib import Path
import struct

import pandas as pd
import pytest

from examples.research.verify_tdx_samples import REFERENCE_COLUMNS, compare_samples
from qbot.research.data.tdx import TdxError
from qbot.research.data.tdx_staging import run_audit


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "vipdoc"
    for m in ("sh", "sz", "bj"):
        (root / m / "lday").mkdir(parents=True)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(struct.pack("<IIIIIfII", 20200102, 1000, 1100, 900, 1050, 10500., 1000, 65536))
    result = run_audit(root, tmp_path / "reports", run_id="original")
    reference = tmp_path / "reference.csv"
    row = dict(zip(REFERENCE_COLUMNS, ["600000.SH", "2020-01-02", 10., 11., 9., 10.5, 10., 1.05,
                                       "CNY", "LOTS_100_SHARES", "TEN_THOUSAND_CNY", "UNADJUSTED",
                                       .005, 0., .001, "synthetic-test-only", "synthetic-test-only-row-1"]))
    pd.DataFrame([row]).to_csv(reference, index=False)
    return Path(result["report_dir"]), reference, tmp_path / "checks"


def test_supplemental_comparison_preserves_original_and_unknown_contract(inputs):
    report, reference, output = inputs
    before = {p.name: p.read_bytes() for p in report.iterdir()}
    generated = compare_samples(report, reference, output, "reference")
    rows = pd.read_csv(generated / "comparisons.csv")
    assert rows.open_candidate_match.all()
    assert rows.volume_candidate_multiplier_1_match.all()
    assert not rows.volume_candidate_multiplier_100_match.any()
    assert rows.amount_candidate_CNY_match.all()
    assert before == {p.name: p.read_bytes() for p in report.iterdir()}
    manifest = json.loads((generated / "manifest.json").read_bytes())
    assert not manifest["financial_research_eligible"]
    assert manifest["network_requests"] == 0


@pytest.mark.parametrize("column,value,match", [
    ("adjustment", "HFQ", "UNADJUSTED_REFERENCE"),
    ("source", "UNKNOWN", "EXPLICIT_REFERENCE_EVIDENCE"),
    ("price_tolerance", -1, "INVALID_REFERENCE_VALUE"),
    ("date", "2020/01/02", "INVALID_REFERENCE_SYMBOL_OR_DATE"),
])
def test_invalid_reference_refused(inputs, column, value, match):
    report, reference, output = inputs
    frame = pd.read_csv(reference)
    frame[column] = value
    frame.to_csv(reference, index=False)
    with pytest.raises(TdxError, match=match):
        compare_samples(report, reference, output)
    assert not output.exists()


def test_unmatched_key_not_claimed_pass(inputs):
    report, reference, output = inputs
    frame = pd.read_csv(reference)
    frame["date"] = "2020-01-03"
    frame.to_csv(reference, index=False)
    generated = compare_samples(report, reference, output)
    assert pd.read_csv(generated / "comparisons.csv").status.iloc[0] == "NO_AUDITED_SAMPLE"


def test_unknown_unit_does_not_guess(inputs):
    report, reference, output = inputs
    frame = pd.read_csv(reference)
    frame["volume_unit"] = "INDEX_VOLUME_UNKNOWN"
    frame.to_csv(reference, index=False)
    generated = compare_samples(report, reference, output)
    assert pd.read_csv(generated / "comparisons.csv").volume_candidate_multiplier_1_match.iloc[0] == "UNKNOWN"


def test_tampered_samples_fail_before_comparison(inputs):
    report, reference, output = inputs
    with (report / "verification_samples.csv").open("ab") as handle:
        handle.write(b"bad")
    with pytest.raises(TdxError, match="SAMPLE_HASH_MISMATCH"):
        compare_samples(report, reference, output)


def test_duplicate_reference_keys_refused(inputs):
    report, reference, output = inputs
    frame = pd.read_csv(reference)
    pd.concat([frame, frame]).to_csv(reference, index=False)
    with pytest.raises(TdxError, match="DUPLICATE_REFERENCE_KEY"):
        compare_samples(report, reference, output)
