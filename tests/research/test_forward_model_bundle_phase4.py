"""Phase 4 leakage guards and immutable forward-model bundle tests."""

import hashlib
import json
import pickle
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from factorlab.forward.model_bundle import (
    BundleExistsError,
    BundleIntegrityError,
    ModelBundleError,
    fit_final_model,
    load_model_bundle,
    save_model_bundle,
)
from factorlab.models import FactorScoreModel, RidgeResearchModel


HASHES = {
    "source_dataset_sha256": "a" * 64,
    "source_sha256": "e" * 64,
    "universe_version": "b" * 64,
    "research_contract_sha256": "c" * 64,
    "git_commit": "d" * 40,
}


def _training_frame():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-01-01",
                    "2026-01-02",
                    "2026-01-03",
                    "2026-01-06",
                    "2026-01-04",
                ]
            ),
            "exit_date": pd.to_datetime(
                [
                    "2026-01-03",
                    "2026-01-04",
                    "2026-01-05",
                    "2026-01-08",
                    "2026-01-06",
                ]
            ),
            "REV1": [1.0, 3.0, 1_000_000.0, 2_000_000.0, 3_000_000.0],
            "MOM20": [2.0, 4.0, 1_000_001.0, 2_000_001.0, 3_000_001.0],
            "EXCESS_RET5": [0.1, 0.2, np.inf, 10.0, 20.0],
        }
    )


def _ridge_fit():
    return fit_final_model(
        _training_frame(),
        model_name="ridge",
        params={"alpha": 0.5},
        feature_names=["REV1", "MOM20"],
        target="EXCESS_RET5",
        training_asof="2026-01-05",
        seed=17,
    )


def _save(fitted, root, version="phase4-v1"):
    return save_model_bundle(fitted, root, version=version, **HASHES)


def _bundle_content_sha256(metadata_sha256, model_sha256):
    digest = hashlib.sha256()
    digest.update(b"qbot-forward-model-bundle-v2\0")
    digest.update(metadata_sha256.encode("ascii"))
    digest.update(b"\0")
    digest.update(model_sha256.encode("ascii"))
    return digest.hexdigest()


def test_fit_final_model_uses_only_mature_finite_labels_before_asof():
    fitted = _ridge_fit()

    assert isinstance(fitted.model, RidgeResearchModel)
    assert fitted.training_rows == 2
    assert fitted.training_asof == "2026-01-05"
    assert fitted.max_label_exit_date == "2026-01-04"
    np.testing.assert_allclose(fitted.model.scaler_.mean_, [2.0, 3.0])
    assert fitted.parameters == {
        "alpha": 0.5,
        "fit_intercept": True,
        "max_iter": 10000,
        "tolerance": 0.0001,
    }


def test_final_fit_requires_one_named_model_and_one_frozen_parameter_mapping():
    frame = _training_frame()
    common = {
        "data": frame,
        "feature_names": ["REV1", "MOM20"],
        "target": "EXCESS_RET5",
        "training_asof": "2026-01-05",
    }

    with pytest.raises(ModelBundleError, match="model_name must be one of"):
        fit_final_model(model_name="best", params={"alpha": 1.0}, **common)
    with pytest.raises(ModelBundleError, match="explicitly contain alpha"):
        fit_final_model(model_name="ridge", params={}, **common)
    with pytest.raises(ModelBundleError, match="unsupported frozen parameters"):
        fit_final_model(
            model_name="ridge",
            params={"alpha": 1.0, "test_selected_alpha": 2.0},
            **common,
        )


def test_factor_score_keeps_exact_fixed_weights():
    frame = _training_frame().replace({np.inf: np.nan})
    fitted = fit_final_model(
        frame,
        model_name="factor_score",
        params={
            "weights": {"MOM20": -0.25, "REV1": 0.75},
            "normalize_by_absolute_weight": True,
        },
        feature_names=["REV1", "MOM20"],
        target="EXCESS_RET5",
        training_asof="2026-01-05",
    )

    assert isinstance(fitted.model, FactorScoreModel)
    assert list(fitted.model.weights) == ["REV1", "MOM20"]
    assert dict(fitted.model.weights) == {"REV1": 0.75, "MOM20": -0.25}


def test_bundle_round_trip_records_provenance_and_exact_feature_order(tmp_path):
    fitted = _ridge_fit()
    saved = _save(fitted, tmp_path)

    assert saved.path == tmp_path / "ridge" / "phase4-v1"
    assert {path.name for path in saved.path.iterdir()} == {
        "model.pkl",
        "metadata.json",
    }
    assert saved.metadata["model_name"] == "ridge"
    assert saved.metadata["model_version"] == "phase4-v1"
    assert saved.metadata["feature_names"] == ["REV1", "MOM20"]
    assert saved.metadata["target"] == "EXCESS_RET5"
    assert saved.metadata["training_rows"] == 2
    assert saved.metadata["source_dataset_sha256"] == "a" * 64
    assert saved.metadata["source_sha256"] == "e" * 64
    assert saved.metadata["universe_version"] == "b" * 64
    assert saved.metadata["research_contract_sha256"] == "c" * 64
    assert saved.metadata["git_commit"] == "d" * 40
    assert saved.metadata["seed"] == 17
    assert saved.metadata["frozen_at"].endswith("Z")

    restored = load_model_bundle(
        saved.path,
        expected_model_name="ridge",
        expected_feature_names=["REV1", "MOM20"],
    )
    scoring = _training_frame().iloc[:2]
    pd.testing.assert_series_equal(
        restored.predict(scoring),
        fitted.predict(scoring),
    )


def test_bundle_version_is_never_overwritten(tmp_path):
    fitted = _ridge_fit()
    saved = _save(fitted, tmp_path)
    before = {
        path.name: path.read_bytes() for path in saved.path.iterdir()
    }

    with pytest.raises(BundleExistsError, match="cannot be overwritten"):
        _save(fitted, tmp_path)

    after = {path.name: path.read_bytes() for path in saved.path.iterdir()}
    assert after == before


def test_load_rejects_modified_pickle_before_deserialising(tmp_path):
    saved = _save(_ridge_fit(), tmp_path)
    model_path = saved.path / "model.pkl"
    model_path.write_bytes(model_path.read_bytes() + b"modified")

    with pytest.raises(BundleIntegrityError, match="model_sha256"):
        load_model_bundle(saved.path)


def test_load_rejects_extra_or_missing_bundle_files(tmp_path):
    saved = _save(_ridge_fit(), tmp_path)
    (saved.path / "notes.txt").write_text("not part of the bundle", encoding="utf-8")

    with pytest.raises(BundleIntegrityError, match="unexpected files"):
        load_model_bundle(saved.path)


def test_load_rejects_model_feature_order_even_with_recomputed_file_hashes(tmp_path):
    saved = _save(_ridge_fit(), tmp_path)
    model_path = saved.path / "model.pkl"
    metadata_path = saved.path / "metadata.json"
    model = pickle.loads(model_path.read_bytes())
    model.feature_names_ = tuple(reversed(model.feature_names_))
    model_payload = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    model_path.write_bytes(model_payload)

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["model_sha256"] = hashlib.sha256(model_payload).hexdigest()
    metadata["content_sha256"] = _bundle_content_sha256(
        metadata["metadata_sha256"], metadata["model_sha256"]
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleIntegrityError, match="feature order"):
        load_model_bundle(saved.path)


def test_load_rejects_wrong_expected_feature_order_without_unpickling(tmp_path):
    saved = _save(_ridge_fit(), tmp_path)

    with pytest.raises(BundleIntegrityError, match="feature order"):
        load_model_bundle(
            saved.path,
            expected_feature_names=["MOM20", "REV1"],
        )


@pytest.mark.parametrize(
    "column,value,message",
    [
        ("date", 20260101, "numeric timestamp"),
        ("exit_date", 20260104, "numeric timestamp"),
        (
            "exit_date",
            pd.Timestamp("2026-01-06 00:30", tz="Asia/Shanghai"),
            "timezone-naive",
        ),
        ("exit_date", pd.Timestamp("2026-01-04 12:00"), "midnight"),
    ],
)
def test_training_dates_reject_ambiguous_numeric_timezone_and_time_values(
    column, value, message
):
    frame = _training_frame().astype({column: object})
    frame.loc[0, column] = value
    with pytest.raises(ModelBundleError, match=message):
        fit_final_model(
            frame,
            model_name="ridge",
            params={"alpha": 0.5},
            feature_names=["REV1", "MOM20"],
            target="EXCESS_RET5",
            training_asof="2026-01-05",
        )


def test_training_cutoff_itself_rejects_numeric_and_timezone_values():
    for cutoff in (
        20260105,
        pd.Timestamp("2026-01-05 00:00", tz="Asia/Shanghai"),
    ):
        with pytest.raises(ModelBundleError, match="numeric timestamp|timezone-naive"):
            fit_final_model(
                _training_frame(),
                model_name="ridge",
                params={"alpha": 0.5},
                feature_names=["REV1", "MOM20"],
                target="EXCESS_RET5",
                training_asof=cutoff,
            )


def test_final_fit_audit_fields_cannot_be_replaced_before_save(tmp_path):
    forged = replace(
        _ridge_fit(),
        training_asof="2025-01-01",
        max_label_exit_date="2025-01-01",
        training_rows=999,
    )
    with pytest.raises(ModelBundleError, match="unmodified result"):
        _save(forged, tmp_path)


def test_load_can_require_exact_source_digest(tmp_path):
    saved = _save(_ridge_fit(), tmp_path)
    with pytest.raises(BundleIntegrityError, match="source digest"):
        load_model_bundle(saved.path, expected_source_sha256="f" * 64)
    restored = load_model_bundle(
        saved.path, expected_source_sha256=HASHES["source_sha256"]
    )
    assert restored.metadata["source_sha256"] == HASHES["source_sha256"]
