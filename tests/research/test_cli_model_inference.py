"""Pinned calendar/score consumers; all market sessions and scores are artificial."""

from dataclasses import replace
from datetime import date
import io

import numpy as np
import pandas as pd
import pytest

from factorlab import cli
from factorlab.config import (
    DataConfig, EvaluationConfig, MarketRegimeConfig, PortfolioConfig,
    ProjectConfig, ResearchConfig, UniverseConfig,
)
from factorlab.errors import FactorLabError
from factorlab.metrics import evaluate_predictions


@pytest.fixture
def bundle(tmp_path):
    days = pd.bdate_range("2024-01-02", periods=65)
    frame = pd.DataFrame({
        "date": np.repeat(days.to_numpy(), 6),
        "symbol": ["%06d.SZ" % (number + 1) for number in range(6)] * len(days),
        "fold": 0,
    })
    values = np.tile(np.arange(6), len(days))
    phases = np.repeat(np.arange(len(days)) / 7, 6)
    frame["score"] = np.sin(values + phases)
    frame["EXCESS_RET5"] = np.cos(values / 2 + phases) / 10
    config = ResearchConfig(
        project=ProjectConfig(name="SYNTHETIC INFERENCE TEST ONLY"),
        data=DataConfig(start=date(2024, 1, 2), end=days[-1].date()),
        universe=UniverseConfig(provider="local", expected_member_count=6),
        factors=("REV1",), evaluation=EvaluationConfig(quantiles=4, min_cross_section=4),
        portfolio=PortfolioConfig(top_k=2), market_regime=MarketRegimeConfig(enabled=False),
    )
    config.validate()
    evaluation = evaluate_predictions(frame, top_k=2, minimum_cross_section=4)
    daily = evaluation.daily.assign(stage="HISTORICAL_TEST")
    artifacts = {}
    for filename, table in (
        ("predictions.parquet", frame), ("stage_prediction_daily.parquet", daily),
        ("evaluation_calendar.parquet", pd.DataFrame({"date": days})),
    ):
        stream = io.BytesIO()
        table.to_parquet(stream, index=False)
        artifacts[filename] = stream.getvalue()
    artifacts["evaluation_calendar.csv"] = pd.DataFrame({"date": days}).to_csv(index=False).encode("utf-8")
    manifest = {
        "config": config.to_dict(), "inference_input_version": 1,
        "inference_calendar_sha256": cli.diagnostic_frame_sha256(pd.DataFrame({"date": days})),
    }
    return config, days, frame, daily, artifacts, manifest


def _replace_parquet(artifacts, filename, table):
    stream = io.BytesIO()
    table.to_parquet(stream, index=False)
    artifacts[filename] = stream.getvalue()


def test_consumers_use_only_verified_bytes_and_recompute_fixed_test_metrics(bundle, monkeypatch):
    config, days, predictions, original_daily, artifacts, manifest = bundle
    def forbidden(*args, **kwargs):
        raise AssertionError("inference reopened a path or called a model")
    monkeypatch.setattr(cli.Path, "read_bytes", forbidden)
    monkeypatch.setattr(cli.Path, "read_text", forbidden)
    monkeypatch.setattr(cli, "_file_sha256", forbidden)
    monkeypatch.setattr(cli, "_load_benchmark_with_warmup", forbidden)
    calendar = cli._verified_inference_calendar("ridge", manifest, artifacts)
    assert calendar.equals(days)
    result = cli._verified_inference_daily(config, "ridge", artifacts)
    assert result.stage.eq("HISTORICAL_TEST").all()
    assert result.model_name.eq("ridge").all()
    assert "regime" not in result
    assert np.array_equal(result.prediction_rank_ic, original_daily.prediction_rank_ic)


def test_unavailable_legacy_calendar_is_explicit(bundle):
    _, _, _, _, artifacts, manifest = bundle
    manifest.pop("inference_input_version")
    assert cli._verified_inference_calendar("ridge", manifest, artifacts) is None


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0])
def test_unknown_calendar_version_rejected(bundle, version):
    _, _, _, _, artifacts, manifest = bundle
    manifest["inference_input_version"] = version
    with pytest.raises(FactorLabError, match="calendar evidence"):
        cli._verified_inference_calendar("ridge", manifest, artifacts)


@pytest.mark.parametrize("mutation", [
    lambda f: f.iloc[::-1], lambda f: pd.concat([f, f.iloc[:1]], ignore_index=True),
    lambda f: f.assign(date=f.date.dt.tz_localize("UTC")),
    lambda f: f.assign(date=f.date + pd.Timedelta(hours=1)),
    lambda f: f.assign(date=f.date.astype(str)), lambda f: f.assign(extra=1),
    lambda f: f.iloc[:0], lambda f: f.assign(date=f.date + pd.Timedelta(days=1000)),
])
def test_bad_calendar_layout_dates_or_resealed_semantics_rejected(bundle, mutation):
    _, days, _, _, artifacts, manifest = bundle
    table = mutation(pd.DataFrame({"date": days}))
    _replace_parquet(artifacts, "evaluation_calendar.parquet", table)
    artifacts["evaluation_calendar.csv"] = table.to_csv(index=False).encode("utf-8")
    manifest["inference_calendar_sha256"] = cli.diagnostic_frame_sha256(table)
    with pytest.raises(FactorLabError, match="calendar evidence"):
        cli._verified_inference_calendar("ridge", manifest, artifacts)


def test_csv_parquet_disagreement_rejected_even_when_byte_inventory_resealed(bundle):
    _, days, _, _, artifacts, manifest = bundle
    artifacts["evaluation_calendar.csv"] = pd.DataFrame({"date": days[:-1]}).to_csv(index=False).encode("utf-8")
    with pytest.raises(FactorLabError, match="calendar evidence"):
        cli._verified_inference_calendar("ridge", manifest, artifacts)


def test_calendar_content_hash_rejects_missing_session(bundle):
    _, days, _, _, artifacts, manifest = bundle
    table = pd.DataFrame({"date": days.delete(7)})
    _replace_parquet(artifacts, "evaluation_calendar.parquet", table)
    artifacts["evaluation_calendar.csv"] = table.to_csv(index=False).encode("utf-8")
    with pytest.raises(FactorLabError, match="semantic SHA"):
        cli._verified_inference_calendar("ridge", manifest, artifacts)


@pytest.mark.parametrize("mutation", [
    lambda f: f.assign(prediction_rank_ic=.99),
    lambda f: f.assign(top_bottom_spread=1.),
    lambda f: f.iloc[1:],
    lambda f: f.assign(stage="TRAIN_IN_SAMPLE"),
    lambda f: f.assign(fold=1),
])
def test_changed_daily_values_cannot_diverge_from_verified_test_scores(bundle, mutation):
    config, _, _, daily, artifacts, _ = bundle
    _replace_parquet(artifacts, "stage_prediction_daily.parquet", mutation(daily))
    with pytest.raises(FactorLabError, match="Test metrics differ"):
        cli._verified_inference_daily(config, "ridge", artifacts)


def test_consumer_honors_explicit_market_regime_and_checks_exact_metric_values(bundle):
    config, _, frame, _, artifacts, _ = bundle
    config = replace(config, market_regime=MarketRegimeConfig(enabled=True))
    frame["MARKET_REGIME"] = "BULL"
    daily = evaluate_predictions(frame, top_k=2, minimum_cross_section=4, regime_col="MARKET_REGIME").daily
    _replace_parquet(artifacts, "predictions.parquet", frame)
    _replace_parquet(artifacts, "stage_prediction_daily.parquet", daily.assign(stage="HISTORICAL_TEST"))
    result = cli._verified_inference_daily(config, "ridge", artifacts)
    assert result.regime.eq("BULL").all()


def test_config_options_preserve_seed_and_label_horizon(bundle):
    config = bundle[0]
    options = cli._inference_options(config)
    assert options.holding_days == config.label.holding_days
    assert options.random_seed == config.seed
    assert options.bootstrap_repetitions == config.inference.bootstrap_repetitions

