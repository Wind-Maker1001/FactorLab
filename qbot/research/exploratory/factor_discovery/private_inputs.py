"""Read-only adapter for the one previously authorized frozen 50-stock dataset."""

from pathlib import Path

import pandas as pd

from qbot.research.exploratory.contract import ExploratoryContractError, load_dataset
from qbot.research.exploratory.runner import verify_receipt as verify_previous_receipt

from .contracts import DiscoveryError, ErrorCode
from .receipts import file_sha256
from .synthetic import frame_sha256

DATA_VERSION = "2f145d173f5ddc79d303f580bc4485241023ae3971d3b40a2e8fa6e3de2e3f5f"
MANIFEST_SHA256 = "c1aef73620bb34f43e1553aa196dcd35fd11eb2abfb34c842b7ff59c5a9b8ced"
APPROVED_RECEIPT_SHA256 = (
    "67b7d1fa82f234565f084b71f795112487e7e7d975693ec3447e8cde191896cc"
)
FROZEN_LABELS_SHA256 = (
    "7544e77456b0cec20615a84a1eb1af0a8e69c8288d6204f27d44881b62f4a0ca"
)


def verify_private_frames(
    bars, benchmark, calendar, labels, universe, evidence, paths=None
):
    """Recheck anchored local files and bind every supplied value to their result."""
    if paths is None:
        root = Path(__file__).resolve().parents[4]
        paths = (
            root / "data/research/exploratory/a-share-50-20261014-v1/manifest.json",
            root / "results/exploratory/2f145d173f5ddc79/89e5d6e2ac7858a8/receipt.json",
        )
    status, approved, reason = load_private_inputs(*paths)
    if status != "SUCCESS":
        raise DiscoveryError(
            ErrorCode.INVALID_INPUT, "private input binding: " + reason
        )
    (
        frozen_bars,
        frozen_benchmark,
        frozen_calendar,
        frozen_labels,
        trusted,
        frozen_universe,
    ) = approved
    try:
        pairs = (
            (bars, frozen_bars),
            (benchmark, frozen_benchmark),
            (pd.DataFrame({"date": calendar}), pd.DataFrame({"date": frozen_calendar})),
            (labels, frozen_labels),
        )
        bound = (
            all(
                frame_sha256(actual) == frame_sha256(expected)
                for actual, expected in pairs
            )
            and list(universe) == list(frozen_universe)
            and evidence == trusted
        )
    except (TypeError, ValueError, AttributeError):
        bound = False
    if not bound:
        raise DiscoveryError(ErrorCode.INVALID_INPUT, "private input binding mismatch")
    return approved, reason


def load_private_inputs(manifest_path=None, frozen_receipt_path=None):
    """Return status plus inputs; absent real evidence is never synthetic success."""
    if manifest_path is None or not Path(manifest_path).is_file():
        return "NOT_RUN", None, "authorized frozen private manifest absent"
    if frozen_receipt_path is None or not Path(frozen_receipt_path).is_file():
        return "BLOCKED", None, "frozen label receipt absent"
    try:
        if file_sha256(manifest_path) != MANIFEST_SHA256:
            raise ExploratoryContractError("frozen manifest byte hash mismatch")
        if file_sha256(frozen_receipt_path) != APPROVED_RECEIPT_SHA256:
            raise ExploratoryContractError("approved v2 receipt byte hash mismatch")
        bars, benchmark, calendar, manifest = load_dataset(
            manifest_path, allow_synthetic=False
        )
        if (
            manifest["data_version"] != DATA_VERSION
            or len(manifest["sample"]["symbols"]) != 50
        ):
            raise ExploratoryContractError("frozen version/sample contract mismatch")
        previous = verify_previous_receipt(frozen_receipt_path)
        if (
            previous.get("status") != "SUCCESS"
            or previous.get("data_version") != DATA_VERSION
            or previous.get("data_files") != manifest["files"]
        ):
            raise ExploratoryContractError("frozen receipt input binding mismatch")
        derived = Path(frozen_receipt_path).parent / "derived_factor_labels.parquet"
        if (
            previous["artifacts"].get(derived.name) != FROZEN_LABELS_SHA256
            or file_sha256(derived) != FROZEN_LABELS_SHA256
        ):
            raise ExploratoryContractError("frozen historical labels hash mismatch")
        frozen = pd.read_parquet(derived)
        required = ["date", "symbol", "entry_date", "exit_date", "EXCESS_RET5"]
        if not set(required).issubset(frozen):
            raise ExploratoryContractError("frozen labels lack explicit dates")
        labels = frozen[required].copy()
        evidence = {
            "classification": "EXPLORATORY / UNVERIFIED / NON_PIT_CONVENIENCE",
            "synthetic_only": False,
            "data_version": DATA_VERSION,
            "manifest_sha256": MANIFEST_SHA256,
            "source_version": manifest["source_version"],
            "verified_source_artifacts": len(manifest["sources"]),
            "input_sha256": {
                key: item["sha256"] for key, item in manifest["files"].items()
            },
            "frozen_label_file_sha256": file_sha256(derived),
            "previous_receipt_sha256": APPROVED_RECEIPT_SHA256,
            "previously_observed_sample": True,
            "formal_admission": False,
        }
        return (
            "SUCCESS",
            (
                bars,
                benchmark,
                calendar,
                labels,
                evidence,
                manifest["sample"]["symbols"],
            ),
            "all frozen private hashes/contracts verified",
        )
    except (ExploratoryContractError, OSError, ValueError, KeyError) as exc:
        return "BLOCKED", None, "private frozen contract failed: " + str(exc)
