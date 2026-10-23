"""Uncertainty reporting is honest even when every primary test is unavailable."""

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from factorlab.metrics.robustness import RobustnessOptions, evaluate_model_robustness
from factorlab.reports.model import ModelReportError, write_model_comparison_report


@pytest.fixture
def result():
    days = pd.bdate_range("2024-01-02", periods=70)
    rng = np.random.default_rng(43)
    frames = []
    for model in ("factor_score", "ridge", "lightgbm", "xgboost"):
        frames.append(pd.DataFrame({
            "date": days, "model_name": model, "fold": 0, "stage": "HISTORICAL_TEST",
            "prediction_ic": rng.uniform(-.2, .2, 70),
            "prediction_rank_ic": rng.uniform(-.2, .2, 70),
            "top_bottom_spread": rng.uniform(-.01, .01, 70),
        }))
    return evaluate_model_robustness(pd.concat(frames, ignore_index=True), days,
                                     RobustnessOptions(bootstrap_repetitions=100))


def _comparison():
    return pd.DataFrame({"model_name": ["factor_score", "ridge", "lightgbm", "xgboost"]})


def test_inference_report_writes_verified_results_and_limitations(result, tmp_path):
    summary = write_model_comparison_report(_comparison(), tmp_path, robustness=result,
                                             disclaimer="SYNTHETIC TEST ONLY")
    text = summary.read_text(encoding="utf-8")
    assert "SYNTHETIC TEST ONLY" in text
    assert "Holm" in text and "统计单位" in text and "双侧" in text
    assert "未做多重比较校正" in text and "不选择模型" in text
    assert "不是净交易 Alpha" in text
    for name, frame in (("model_inference", result.summary), ("inference_daily", result.daily), ("inference_audit", result.audit)):
        pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / (name + ".parquet")), frame)
        assert (tmp_path / (name + ".csv")).is_file()
    protocol = json.loads((tmp_path / "inference_protocol.json").read_text(encoding="utf-8"))
    assert protocol["protocol"] == result.protocol and protocol["evidence"] == result.evidence
    assert (tmp_path / "model_inference.png").stat().st_size > 1000


@pytest.mark.parametrize("name", ["daily", "summary", "audit"])
def test_changed_produced_tables_fail_before_output(result, tmp_path, name):
    table = getattr(result, name).copy()
    table[table.columns[0]] = table.iloc[:, 0].astype(object)
    table.iloc[0, 0] = "changed"
    changed = replace(result, **{name: table})
    target = tmp_path / "must-not-exist"
    with pytest.raises(ModelReportError, match="produced evidence"):
        write_model_comparison_report(_comparison(), target, robustness=changed)
    assert not target.exists()


def test_unknown_protocol_cannot_be_reported(result, tmp_path):
    protocol = {**result.protocol, "primary_metric": "portfolio_return"}
    with pytest.raises(ModelReportError, match="unknown inference protocol"):
        write_model_comparison_report(_comparison(), tmp_path / "bad", robustness=replace(result, protocol=protocol))


def test_inference_and_comparison_must_share_model_identities(result, tmp_path):
    with pytest.raises(ModelReportError, match="identities"):
        write_model_comparison_report(_comparison().iloc[:2], tmp_path / "bad", robustness=result)


def test_reserved_inference_tables_require_the_typed_result(result, tmp_path):
    with pytest.raises(ModelReportError, match="verified robustness"):
        write_model_comparison_report(_comparison(), tmp_path / "bad", details={"model_inference": result.summary})


def test_legacy_report_does_not_invent_significance(tmp_path):
    summary = write_model_comparison_report(_comparison(), tmp_path)
    assert "ICIR 不是显著性检验" in summary.read_text(encoding="utf-8")
    assert not (tmp_path / "model_inference.csv").exists()

