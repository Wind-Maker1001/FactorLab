"""Verify shipped derived artifacts; source prices remain private.

These checks validate receipt integrity and disclosures, not financial
qualification, historic PIT membership, or the vendor HFQ algorithm.
"""

from pathlib import Path

from PIL import Image
import pytest

from factorlab.exploratory.contract import REQUIRED_MARKERS
from factorlab.exploratory.publication import verify_publication


EVIDENCE = Path(__file__).resolve().parents[2] / "docs/research/evidence"


@pytest.mark.parametrize(
    "generation", ["exploratory-factor-v1", "exploratory-factor-v2"]
)
def test_shipped_publication_survives_checkout_with_verified_derived_hashes(generation):
    receipt = verify_publication(EVIDENCE / generation / "receipt.json")
    assert receipt["status"] == "SUCCESS"
    assert receipt["formal_admission"] is False
    assert receipt["synthetic_only"] is False
    assert receipt["forward_allowed"] is False
    assert receipt["live_allowed"] is False
    assert receipt["raw_source_bytes_published"] is False


@pytest.mark.parametrize(
    "generation", ["exploratory-factor-v1", "exploratory-factor-v2"]
)
def test_shipped_report_and_all_pngs_keep_exploratory_disclosures(generation):
    publication = EVIDENCE / generation
    text = (publication / "summary.md").read_text(encoding="utf-8")
    figures = sorted(publication.glob("*.png"))
    assert len(figures) == 9
    assert all(marker in text for marker in REQUIRED_MARKERS)
    for path in figures:
        with Image.open(path) as figure:
            assert figure.width >= 1760 and figure.height >= 800
            assert all(
                marker in figure.info.get("Description", "")
                for marker in REQUIRED_MARKERS
            )
