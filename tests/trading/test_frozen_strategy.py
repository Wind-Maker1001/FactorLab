"""Real frozen-model deployment, causal scoring, and strict binding contracts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from factorlab.forward.model_bundle import fit_final_model, save_model_bundle, load_model_bundle
from factorlab.models.factory import FeatureRef, ModelSpec, create_model, validate_spec
from factorlab.trading.strategy import ClosedBar
from factorlab.trading.frozen_strategy import (
    FixedScoreConversion, FrozenDeploymentBinding, FrozenFeature, FrozenModelStrategy,
    deployment_contract_sha256,
)

HASHES = dict(source_dataset_sha256="a" * 64, source_sha256="b" * 64,
              universe_version="c" * 64, research_contract_sha256="d" * 64,
              git_commit="e" * 40)
FEATURES = (FrozenFeature("r1", "return"), FrozenFeature("r2", "return", 2))
HASHES["research_contract_sha256"] = deployment_contract_sha256(
    "BINANCE_USDM", "NVDAUSDT", 3600, "BINANCE_USDM_PUBLIC_REST", FEATURES,
)


def binding():
    return FrozenDeploymentBinding("BINANCE_USDM", "NVDAUSDT", 3600,
                                   "BINANCE_USDM_PUBLIC_REST", HASHES["source_sha256"],
                                   HASHES["research_contract_sha256"], FEATURES)


def bars():
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [ClosedBar("NVDAUSDT", start + timedelta(hours=i), start + timedelta(hours=i + 1),
                      Decimal(price), Decimal(price), Decimal(price), Decimal(price),
                      Decimal(100), 3600, "BINANCE_USDM_PUBLIC_REST")
            for i, price in enumerate(("100", "101", "104", "102"))]


@pytest.fixture(params=["factor_score", "ridge", "lightgbm", "xgboost"])
def bundle(request, tmp_path):
    name = request.param
    if name in {"lightgbm", "xgboost"}:
        pytest.importorskip(name)
    parameters = {"factor_score": {"weights": {"r1": 1., "r2": .5}},
                  "ridge": {"alpha": .5},
                  "lightgbm": {"n_estimators": 3, "min_child_samples": 2, "n_jobs": 1},
                  "xgboost": {"n_estimators": 3, "n_jobs": 1}}[name]
    refs = tuple(FeatureRef(f.column, f.column, "1", "f" * 64) for f in FEATURES)
    factory_parameters = {key: value for key, value in parameters.items() if key != "n_jobs"}
    spec = validate_spec(ModelSpec(name, "legacy-v1", factory_parameters, refs,
                                   None if name == "factor_score" else 17, 1, "research_v2"))
    factory_model = create_model(spec)
    frame = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=16),
                          "exit_date": pd.date_range("2026-01-02", periods=16),
                          "r1": np.linspace(-.1, .1, 16),
                          "r2": np.linspace(.2, -.2, 16),
                          "target": np.linspace(-.02, .02, 16)})
    factory_model.fit(frame[["r1", "r2"]], frame["target"])
    fitted = fit_final_model(frame, model_name=name, params=parameters,
                             feature_names=["r1", "r2"], target="target",
                             training_asof="2026-02-01", seed=17)
    saved = save_model_bundle(fitted, tmp_path, version="paper-v1", **HASHES)
    loaded = load_model_bundle(saved)
    np.testing.assert_allclose(factory_model.predict(frame), loaded.predict(frame))
    return loaded


def strategy(bundle, **overrides):
    kwargs = dict(strategy_id="frozen", strategy_version="1", expected_model_id=bundle.model_name,
                  expected_model_version=bundle.model_version,
                  expected_bundle_id=bundle.metadata["content_sha256"], binding=binding(),
                  conversion=FixedScoreConversion(.01, -.01, Decimal(".1")))
    kwargs.update(overrides)
    return FrozenModelStrategy(bundle, **kwargs)


def test_actual_bundle_scores_exact_causal_features_and_round_trips(bundle):
    history = bars()[:3]
    adapter = strategy(bundle)
    now = history[-1].closed_at
    signal = adapter.signal(history, now)
    frame = pd.DataFrame([[104 / 101 - 1, 104 / 100 - 1]], columns=["r1", "r2"])
    assert signal.score == pytest.approx(float(bundle.predict(frame).iloc[0]))
    assert signal.model_id == bundle.model_name
    assert signal.model_bundle_id == bundle.metadata["content_sha256"]
    assert signal.input_watermark == now
    assert adapter.history_limit == 3
    assert adapter.signal(history, now + timedelta(minutes=1)).signal_id == signal.signal_id
    assert strategy(bundle, **{}).describe() == adapter.describe()
    kwargs = dict(strategy_id="frozen", strategy_version="1", expected_model_id=bundle.model_name,
                  expected_model_version=bundle.model_version, expected_bundle_id=bundle.metadata["content_sha256"],
                  binding=binding(), conversion=FixedScoreConversion(.01, -.01, Decimal(".1")))
    assert FrozenModelStrategy(bundle.path, **kwargs).signal(history, now) == signal
    warmup = adapter.signal(history[:2], now)
    assert (warmup.direction, warmup.status, warmup.score) == ("HOLD", "INSUFFICIENT_DATA", None)


@pytest.mark.parametrize("field,value", [("expected_model_version", "wrong"),
                                            ("expected_bundle_id", "0" * 64)])
def test_expected_identity_mismatch_rejected(bundle, field, value):
    with pytest.raises(ValueError, match="mismatch"):
        strategy(bundle, **{field: value})


@pytest.mark.parametrize("change", [dict(source_sha256="0" * 64),
                                     dict(research_contract_sha256="0" * 64),
                                     dict(features=FEATURES[::-1])])
def test_binding_hash_and_order_mismatch_rejected(bundle, change):
    with pytest.raises(ValueError, match="mismatch"):
        strategy(bundle, binding=replace(binding(), **change))


@pytest.mark.parametrize("day_offset", [-1, 0])
def test_input_watermark_must_follow_training_asof_day(bundle, day_offset):
    history = bars()[:3]
    cutoff = datetime.fromisoformat(bundle.metadata["training_asof"]).replace(tzinfo=timezone.utc)
    watermark = cutoff + timedelta(days=day_offset, hours=23)
    shift = watermark - history[-1].closed_at
    earlier = [replace(bar, opened_at=bar.opened_at + shift, closed_at=bar.closed_at + shift)
               for bar in history]
    with pytest.raises(ValueError, match="after the training_asof day"):
        strategy(bundle).signal(earlier, watermark)
    next_day = [replace(bar, opened_at=bar.opened_at + timedelta(days=2),
                        closed_at=bar.closed_at + timedelta(days=2)) for bar in earlier]
    assert strategy(bundle).signal(next_day, next_day[-1].closed_at).score is not None


def test_market_source_interval_and_causality_rejected(bundle):
    history = bars()[:3]
    adapter = strategy(bundle)
    now = history[-1].closed_at
    for change in (dict(symbol="OTHER"), dict(source="OTHER"), dict(complete=False)):
        with pytest.raises(ValueError):
            adapter.signal(history[:-1] + [replace(history[-1], **change)], now)
    with pytest.raises(ValueError, match="already closed"):
        adapter.signal(bars(), now)
    with pytest.raises(ValueError, match="contiguous"):
        adapter.signal([history[0], history[2]], now)
    with pytest.raises(ValueError, match="timezone"):
        adapter.signal(history, now.replace(tzinfo=None))
    with pytest.raises(ValueError, match="digest mismatch"):
        strategy(bundle, binding=replace(binding(), interval_seconds=60)).signal(history, now)


@pytest.mark.parametrize("change", [
    dict(source="OTHER"), dict(market="A_SHARE"), dict(symbol="OTHER"),
    dict(features=(FEATURES[0], replace(FEATURES[1], lookback=3))),
])
def test_deployment_field_drift_rejected_by_contract_digest(change):
    with pytest.raises(ValueError, match="digest mismatch"):
        replace(binding(), **change)


def test_market_restriction_applies_even_with_matching_digest():
    digest = deployment_contract_sha256(
        "A_SHARE", "NVDAUSDT", 3600, "BINANCE_USDM_PUBLIC_REST", FEATURES,
    )
    with pytest.raises(ValueError, match="unsupported deployment"):
        replace(binding(), market="A_SHARE", research_contract_sha256=digest)


def test_changed_feature_definition_cannot_reuse_bundle_contract(bundle):
    features = (FEATURES[0], replace(FEATURES[1], lookback=3))
    digest = deployment_contract_sha256(
        "BINANCE_USDM", "NVDAUSDT", 3600, "BINANCE_USDM_PUBLIC_REST", features,
    )
    changed = replace(binding(), features=features, research_contract_sha256=digest)
    with pytest.raises(ValueError, match="research_contract_sha256"):
        strategy(bundle, binding=changed)


def test_fixed_conversion_is_long_only_and_independent_of_model():
    conversion = FixedScoreConversion(.1, -.1, Decimal(".2"))
    assert conversion.convert(.11) == ("BUY", Decimal(".2"))
    assert conversion.convert(-.11) == ("SELL", Decimal(0))
    assert conversion.convert(0) == ("HOLD", None)
    assert conversion.convert(.1) == ("HOLD", None)
    assert conversion.convert(-.1) == ("HOLD", None)
    with pytest.raises(ValueError):
        FixedScoreConversion(-.1, .1, Decimal(".2"))


def test_fixed_features_use_only_trailing_closed_values():
    history = bars()
    assert FrozenFeature("x", "return", 2).value(history) == pytest.approx(102 / 101 - 1)
    returns = np.array([104 / 101 - 1, 102 / 104 - 1])
    assert FrozenFeature("x", "mean_return", 2).value(history) == pytest.approx(returns.mean())
    assert FrozenFeature("x", "volatility", 2).value(history) == pytest.approx(returns.std())
    assert FrozenFeature("x", "volume").value(history) == 100
    with pytest.raises(ValueError, match="unsupported"):
        FrozenFeature("x", "future_return")
