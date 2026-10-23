"""Model comparison consumes the same report bytes that passed hash checks."""

import io
from pathlib import Path

import pandas as pd
import pytest

from qbot.research import cli
from qbot.research.errors import FactorLabError


@pytest.fixture
def report(tmp_path):
    output = tmp_path / "ridge" / "verified-run"
    output.mkdir(parents=True)
    predictions = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "symbol": ["600000.SH", "000001.SZ"], "fold": [1, 1],
        "entry_date": pd.to_datetime(["2024-01-03", "2024-01-04"]),
        "exit_date": pd.to_datetime(["2024-01-10", "2024-01-11"]),
        "EXCESS_RET5": [.02, -.01], "prediction": [.01, -.005],
    })
    predictions.to_parquet(output / "predictions.parquet", index=False)
    pd.DataFrame({"scope": ["OVERALL"], "period": ["ALL"], "RankIC Mean": [.25]}).to_csv(
        output / "prediction_summary.csv", index=False
    )
    pd.DataFrame({"metric": ["Cumulative Return"], "value": [.10]}).to_csv(
        output / "portfolio_metrics.csv", index=False
    )
    pd.DataFrame({"metric": ["Cumulative Return"], "value": [.08]}).to_csv(
        output / "execution_portfolio_metrics.csv", index=False
    )
    pd.DataFrame({"date": ["2024-01-03"], "nav": [1.]}).to_csv(
        output / "execution_nav.csv", index=False
    )
    pd.DataFrame(columns=["date", "symbol", "reason"]).to_csv(
        output / "execution_rejections.csv", index=False
    )
    pd.DataFrame({"fold": [1], "RankIC Mean": [.25]}).to_csv(
        output / "walk_forward_results.csv", index=False
    )
    (output / "summary.md").write_text("# Verified research report\n", encoding="utf-8")
    (output / "nav_curve.png").write_bytes(b"image fixture; hashed but not parsed")
    inventory = cli._artifact_sha256(output)
    manifest = {
        "model_artifact_sha256": inventory,
        "predictions_sha256": inventory["predictions.parquet"],
    }
    return output, manifest, predictions


def _forbid_post_verification_path_reads(monkeypatch):
    """Allow normal parsers, but require them to receive the verified bytes."""

    read_csv, read_parquet = pd.read_csv, pd.read_parquet
    def csv_reader(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO), "CSV parser re-opened an unverified path"
        return read_csv(source, *args, **kwargs)
    def parquet_reader(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO), "Parquet parser re-opened an unverified path"
        return read_parquet(source, *args, **kwargs)
    def unexpected(*args, **kwargs):
        raise AssertionError("post-verification filesystem read")
    monkeypatch.setattr(pd, "read_csv", csv_reader)
    monkeypatch.setattr(pd, "read_parquet", parquet_reader)
    monkeypatch.setattr(cli, "_file_sha256", unexpected)
    monkeypatch.setattr(Path, "read_bytes", unexpected)
    monkeypatch.setattr(Path, "is_file", unexpected)


def test_verifier_returns_tabular_bytes_only_and_checks_all_artifacts(report, monkeypatch):
    output, manifest, _ = report
    expected = {
        name: (output / name).read_bytes()
        for name in manifest["model_artifact_sha256"]
        if name.endswith((".csv", ".parquet"))
    }
    read_bytes, file_sha256 = Path.read_bytes, cli._file_sha256
    read_counts = {}
    def capture_then_replace(path):
        content = read_bytes(path)
        if path.suffix in {".csv", ".parquet"}:
            read_counts[path.name] = read_counts.get(path.name, 0) + 1
            path.write_bytes(b"replaced after snapshot capture, before hash verification")
        return content
    def streamed_hash(path):
        assert path.suffix not in {".csv", ".parquet"}, "tabular hashing re-opened a path"
        return file_sha256(path)
    monkeypatch.setattr(Path, "read_bytes", capture_then_replace)
    monkeypatch.setattr(cli, "_file_sha256", streamed_hash)
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    assert snapshots == expected
    assert all(isinstance(value, bytes) for value in snapshots.values())
    assert "summary.md" not in snapshots
    assert "nav_curve.png" not in snapshots
    assert read_counts == {name: 1 for name in expected}


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, None])
def test_calendar_producer_version_is_strict_before_artifact_consumption(report, version):
    output, manifest, _ = report
    manifest.update(inference_input_version=version, inference_calendar_sha256="a" * 64)
    with pytest.raises(FactorLabError, match="calendar policy/version"):
        cli._verify_model_artifacts("ridge", output, manifest)


def test_calendar_producer_requires_both_inventory_artifacts(report):
    output, manifest, _ = report
    manifest.update(inference_input_version=1, inference_calendar_sha256="a" * 64)
    with pytest.raises(FactorLabError, match="inventory is missing"):
        cli._verify_model_artifacts("ridge", output, manifest)


def test_calendar_cannot_masquerade_as_legacy_without_producer_version(report):
    output, manifest, _ = report
    (output / "evaluation_calendar.csv").write_text("date\n2024-01-02\n", encoding="utf-8")
    manifest["model_artifact_sha256"] = cli._artifact_sha256(output)
    with pytest.raises(FactorLabError, match="lack producer provenance"):
        cli._verify_model_artifacts("ridge", output, manifest)


@pytest.mark.parametrize("filename", ["predictions.parquet", "portfolio_metrics.csv", "nav_curve.png"])
def test_verifier_rejects_changed_tabular_and_streamed_artifacts(report, filename):
    output, manifest, _ = report
    path = output / filename
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(FactorLabError, match="artifact SHA-256 mismatch"):
        cli._verify_model_artifacts("ridge", output, manifest)


def test_prediction_keys_use_verified_bytes_after_prediction_path_changes(report, monkeypatch):
    output, manifest, predictions = report
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    (output / "predictions.parquet").write_bytes(b"invalid replacement after verification")
    _forbid_post_verification_path_reads(monkeypatch)
    actual = cli._prediction_oos_keys(
        "ridge", output, manifest, "EXCESS_RET5", content=snapshots["predictions.parquet"]
    )
    columns = ["date", "symbol", "fold", "entry_date", "exit_date", "EXCESS_RET5"]
    expected = predictions[columns].sort_values(["date", "symbol", "fold"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(actual, expected)


def test_comparison_summary_uses_pinned_csvs_after_all_metric_paths_change(report, monkeypatch):
    output, manifest, _ = report
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    for filename in ["prediction_summary.csv", "portfolio_metrics.csv", "execution_portfolio_metrics.csv"]:
        (output / filename).write_text("invalid,replacement\n", encoding="utf-8")
    _forbid_post_verification_path_reads(monkeypatch)
    actual = cli._comparison_record("ridge", output, artifacts=snapshots)
    assert actual["model_name"] == "ridge"
    assert actual["run_id"] == output.name
    assert actual["RankIC Mean"] == pytest.approx(.25)
    assert actual["Cumulative Return"] == pytest.approx(.10)
    assert actual["execution_Cumulative Return"] == pytest.approx(.08)


def test_combined_tables_use_pinned_csv_after_table_path_changes(report, monkeypatch):
    output, manifest, _ = report
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    (output / "walk_forward_results.csv").write_text("fold,RankIC Mean\n999,-1\n", encoding="utf-8")
    _forbid_post_verification_path_reads(monkeypatch)
    actual = cli._combine_model_tables(
        {"ridge": output}, "walk_forward_results.csv", required=True,
        artifact_snapshots={"ridge": snapshots},
    )
    assert actual["fold"].tolist() == [1]
    assert actual["RankIC Mean"].tolist() == [.25]
    assert actual["source_run_id"].tolist() == [output.name]
    assert actual["model_name"].tolist() == ["ridge"]


def test_optional_csv_newly_added_after_verification_is_not_consumed(report, monkeypatch):
    output, manifest, _ = report
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    (output / "optional_late.csv").write_text("metric,value\nalpha,999\n", encoding="utf-8")
    _forbid_post_verification_path_reads(monkeypatch)
    actual = cli._combine_model_tables(
        {"ridge": output}, "optional_late.csv", required=False,
        artifact_snapshots={"ridge": snapshots},
    )
    assert actual.empty
    assert actual.columns.tolist() == ["source_run_id", "model_name"]


def test_required_csv_absent_from_snapshot_cannot_be_rescued_by_new_file(report, monkeypatch):
    output, manifest, _ = report
    snapshots = cli._verify_model_artifacts("ridge", output, manifest)
    del snapshots["walk_forward_results.csv"]
    _forbid_post_verification_path_reads(monkeypatch)
    with pytest.raises(FactorLabError, match="missing walk_forward_results.csv"):
        cli._combine_model_tables(
            {"ridge": output}, "walk_forward_results.csv", required=True,
            artifact_snapshots={"ridge": snapshots},
        )
