"""The Phase-4 example must stay synthetic, offline, and append-only."""

import json

import pandas as pd
import pytest

from examples.research.phase4_synthetic_demo import (
    ACTIVATION_DATE,
    FEATURE_NAMES,
    MODEL_VERSION,
    REISSUE_REASON,
    TOP_K,
    TRAINING_ASOF,
    WARNING,
    run_demo,
)
from qbot.research.forward import (
    load_forward_activation,
    load_model_bundle,
    load_prediction_record,
    load_signal_record,
)


def test_phase4_synthetic_demo_runs_the_low_level_forward_lifecycle(tmp_path):
    destination = tmp_path / "phase4_synthetic"
    manifest = run_demo(destination, seed=42)
    stored = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))

    assert stored == manifest
    assert stored["warning"] == WARNING
    assert stored["synthetic"] is True
    assert stored["software_demo_only"] is True
    assert stored["research_result"] is False
    assert stored["network_used"] is False
    assert stored["cli_used"] is False
    assert stored["pit_used"] is False
    assert stored["real_data_used"] is False
    assert stored["live_trading"] is False
    assert WARNING in (destination / "README.md").read_text(encoding="utf-8")
    assert WARNING in (
        destination / "SYNTHETIC_SOFTWARE_DEMO_ONLY.txt"
    ).read_text(encoding="utf-8")

    activation_details = stored["activation"]
    activation = load_forward_activation(
        destination / "forward",
        activation_details["activation_id"],
        expected_contract_sha256=activation_details["contract_sha256"],
    )
    assert activation.activation_date == ACTIVATION_DATE
    assert activation.manifest_sha256 == activation_details["manifest_sha256"]
    assert activation_details["registered_in_advance"] is True
    assert activation_details["load_verified"] is True

    model_details = stored["ridge_model"]
    bundle = load_model_bundle(
        destination / model_details["directory"],
        expected_model_name="ridge",
        expected_feature_names=FEATURE_NAMES,
    )
    assert bundle.model_version == MODEL_VERSION
    assert bundle.metadata["training_asof"] == TRAINING_ASOF.isoformat()
    assert bundle.metadata["max_label_exit_date"] <= TRAINING_ASOF.isoformat()
    assert model_details["mature_training_rows"] == bundle.metadata["training_rows"]
    assert model_details["excluded_immature_rows"] > 0
    # Extreme immature features are around +/-10,000.  A small scaler mean
    # proves they were removed before Ridge preprocessing was fitted.
    assert max(abs(value) for value in bundle.model.scaler_.mean_) < 2.0

    frames = {}
    expected = {
        "on_time": ("ON_TIME", "", "v000001"),
        "late_reissue": ("LATE_REISSUE", REISSUE_REASON, "v000002"),
    }
    for name, (timeliness, reason, version) in expected.items():
        details = stored["signals"][name]
        loaded = load_signal_record(destination / details["directory"])
        frame = pd.read_csv(loaded.signals_path, keep_default_na=False)
        predictions = load_prediction_record(loaded.directory)
        frames[name] = frame

        assert loaded.version == details["version"] == version
        assert loaded.row_count == details["rows"] == TOP_K
        assert loaded.sha256 == details["sha256"]
        assert loaded.manifest_sha256 == details["manifest_sha256"]
        assert details["load_signal_record_verified"] is True
        assert details["load_prediction_record_verified"] is True
        assert len(predictions) == loaded.prediction_row_count == details["prediction_rows"]
        assert len(predictions) > TOP_K
        assert loaded.predictions_sha256 == details["prediction_sha256"]
        assert predictions["rank"].tolist() == list(range(1, len(predictions) + 1))
        pd.testing.assert_frame_equal(
            predictions[["symbol", "score", "rank"]].head(TOP_K).reset_index(drop=True),
            frame[["symbol", "score", "rank"]],
        )
        assert frame["timeliness"].unique().tolist() == [timeliness]
        assert frame["reissue_reason"].unique().tolist() == [reason]
        assert frame["rank"].tolist() == list(range(1, TOP_K + 1))
        assert frame["score"].is_monotonic_decreasing
        assert frame["target_weight"].sum() == pytest.approx(1.0)
        assert frame["signal_date"].unique().tolist() == [
            ACTIVATION_DATE.isoformat()
        ]

    assert frames["on_time"]["symbol"].tolist() == stored["signals"][
        "selected_symbols"
    ]
    assert frames["late_reissue"]["symbol"].tolist() == frames["on_time"][
        "symbol"
    ].tolist()
    assert frames["late_reissue"]["score"].tolist() == pytest.approx(
        frames["on_time"]["score"].tolist()
    )


def test_phase4_synthetic_demo_refuses_a_nonempty_output_directory(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep-me.txt"
    sentinel.write_bytes(b"do not replace\n")
    before = {path.name: path.read_bytes() for path in destination.iterdir()}

    with pytest.raises(FileExistsError, match="not empty"):
        run_demo(destination, seed=42)

    after = {path.name: path.read_bytes() for path in destination.iterdir()}
    assert after == before
