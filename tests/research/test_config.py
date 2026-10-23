from pathlib import Path

import pytest
import yaml

from qbot.research.config import load_config
from qbot.research.errors import (
    ConfigurationError,
    POINT_IN_TIME_UNIVERSE_REQUIRED,
    PointInTimeUniverseRequiredError,
)


def _valid_document():
    return {
        "project": {"name": "test-factorlab"},
        "data": {
            "start": "2024-01-02",
            "end": "2024-04-30",
            "provider": "akshare",
            "benchmark": "000905.SH",
            "adjustment": "hfq",
            "calendar_source": "benchmark",
        },
        "universe": {
            "name": "CSI500",
            "index_symbol": "000905.SH",
            "provider": "baostock",
            "point_in_time": True,
            "expected_member_count": 500,
        },
        "factors": ["REV1", "MOM20"],
    }


def _write_config(root: Path, document) -> Path:
    config_dir = root / "configs" / "research"
    config_dir.mkdir(parents=True)
    path = config_dir / "test.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_load_config_is_strict_and_resolves_paths_from_repo_root(tmp_path):
    document = _valid_document()
    document["paths"] = {
        "data_root": "var/research-data",
        "results_root": "var/results",
    }
    path = _write_config(tmp_path, document)

    config = load_config(path.relative_to(tmp_path), repo_root=tmp_path)

    assert config.paths.data_root == (tmp_path / "var/research-data").resolve()
    assert config.paths.results_root == (tmp_path / "var/results").resolve()
    assert config.config_path == path.resolve()
    assert config.repo_root == tmp_path.resolve()
    assert config.factors == ("REV1", "MOM20")
    assert config.data.start.isoformat() == "2024-01-02"


@pytest.mark.parametrize(
    "mutate,unknown_name",
    [
        (lambda value: value.update({"surprise": True}), "surprise"),
        (
            lambda value: value["data"].update({"future_fill": "bfill"}),
            "future_fill",
        ),
    ],
)
def test_unknown_fields_are_rejected(tmp_path, mutate, unknown_name):
    document = _valid_document()
    mutate(document)
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match=unknown_name):
        load_config(path, repo_root=tmp_path)


def test_missing_required_field_is_rejected(tmp_path):
    document = _valid_document()
    document.pop("data")
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="missing required field.*data"):
        load_config(path, repo_root=tmp_path)


def test_non_boolean_is_not_coerced(tmp_path):
    document = _valid_document()
    document["live_trading"] = "false"
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="live_trading must be a boolean"):
        load_config(path, repo_root=tmp_path)


def test_point_in_time_universe_cannot_be_disabled(tmp_path):
    document = _valid_document()
    document["universe"]["point_in_time"] = False
    path = _write_config(tmp_path, document)

    with pytest.raises(PointInTimeUniverseRequiredError) as caught:
        load_config(path, repo_root=tmp_path)

    assert POINT_IN_TIME_UNIVERSE_REQUIRED in str(caught.value)


def test_formal_and_smoke_configs_load():
    repository = Path(__file__).resolve().parents[2]

    formal = load_config("configs/research/csi500_5y.yaml", repo_root=repository)
    smoke = load_config("configs/research/csi500_smoke.yaml", repo_root=repository)

    assert formal.data.start.isoformat() == "2021-09-13"
    assert formal.data.end.isoformat() == "2026-09-11"
    assert formal.data.warmup_trading_days == 130
    assert formal.universe.status_provider == "baostock"
    assert formal.live_trading is False
    assert formal.validation.initial_train_months == 24
    assert formal.validation.purge_days == 5
    assert formal.portfolio.top_k == 20
    assert formal.models.ridge.alphas == (0.1, 1.0, 10.0, 100.0)
    assert len(formal.models.lightgbm.candidates) == 2
    assert len(formal.models.xgboost.candidates) == 2
    assert formal.market_regime.enabled is True
    assert formal.backtest.t_plus_one is True
    assert formal.backtest.board_lot == 100
    assert len(formal.backtest.price_limits) == 4
    assert set(formal.models.factor_score.weights) == set(formal.factors)
    assert formal.fees.stamp_duty[0].sell_bps == 10.0
    assert formal.fees.stamp_duty[1].sell_bps == 5.0
    assert smoke.project.name.endswith("smoke-test-only")
    assert smoke.data.end < formal.data.end
    assert smoke.data.end.isoformat() == "2024-05-17"
    assert smoke.validation.initial_train_months == 2


def test_security_status_provider_must_be_explicitly_supported(tmp_path):
    document = _valid_document()
    document["universe"]["status_provider"] = "current-snapshot"
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="status_provider"):
        load_config(path, repo_root=tmp_path)


def test_phase1_rejects_qfq_because_incremental_history_can_be_rewritten(tmp_path):
    document = _valid_document()
    document["data"]["adjustment"] = "qfq"
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="must be hfq"):
        load_config(path, repo_root=tmp_path)


def test_warmup_must_cover_listing_and_quality_windows(tmp_path):
    document = _valid_document()
    document["data"]["warmup_trading_days"] = 10
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="warmup_trading_days must cover"):
        load_config(path, repo_root=tmp_path)


def test_manifest_snapshot_is_json_serializable(tmp_path):
    config = load_config(_write_config(tmp_path, _valid_document()), repo_root=tmp_path)

    snapshot = config.to_dict()

    assert snapshot["data"]["start"] == "2024-01-02"
    assert snapshot["factors"] == ["REV1", "MOM20"]
    assert "repo_root" not in snapshot
    assert "config_path" not in snapshot


def test_phase2_nested_unknown_field_is_rejected(tmp_path):
    document = _valid_document()
    document["models"] = {"ridge": {"alphas": [1.0], "future_hint": True}}
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="future_hint"):
        load_config(path, repo_root=tmp_path)


def test_phase3_tree_candidate_unknown_field_is_rejected(tmp_path):
    document = _valid_document()
    document["models"] = {
        "lightgbm": {
            "candidates": [{"n_estimators": 10, "tune_on_test": True}]
        }
    }
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="tune_on_test"):
        load_config(path, repo_root=tmp_path)


def test_price_limit_rules_must_cover_every_supported_board(tmp_path):
    document = _valid_document()
    document["backtest"] = {
        "price_limits": [
            {
                "board": "main",
                "start": "1990-01-01",
                "end": None,
                "normal_limit_pct": 0.1,
                "st_limit_pct": 0.05,
            }
        ]
    }
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="cover board"):
        load_config(path, repo_root=tmp_path)


@pytest.mark.parametrize(
    "section,field,bad_symbol",
    [
        ("data", "benchmark", "000300.SH"),
        ("universe", "index_symbol", "000300.SH"),
        ("data", "benchmark", "sh000905"),
    ],
)
def test_csi500_benchmark_and_universe_symbol_are_fixed(
    tmp_path, section, field, bad_symbol
):
    document = _valid_document()
    document[section][field] = bad_symbol
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="both be canonical 000905.SH"):
        load_config(path, repo_root=tmp_path)


def test_factor_score_weights_must_match_enabled_factors(tmp_path):
    document = _valid_document()
    document["models"] = {
        "factor_score": {"weights": {"REV1": 1.0}},
    }
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="must match the enabled factor list"):
        load_config(path, repo_root=tmp_path)


def test_unregistered_factor_is_rejected_before_pipeline_execution(tmp_path):
    document = _valid_document()
    document["factors"] = ["REV1", "NOT_A_REAL_FACTOR"]
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="unregistered names"):
        load_config(path, repo_root=tmp_path)


def test_purge_must_cover_forward_label_horizon(tmp_path):
    document = _valid_document()
    document["validation"] = {"purge_days": 4}
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="purge_days must be at least"):
        load_config(path, repo_root=tmp_path)


def test_stamp_duty_schedule_must_be_contiguous(tmp_path):
    document = _valid_document()
    document["fees"] = {
        "stamp_duty": [
            {"start": "2021-01-01", "end": "2023-08-27", "sell_bps": 10},
            {"start": "2023-08-29", "end": None, "sell_bps": 5},
        ]
    }
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="contiguous"):
        load_config(path, repo_root=tmp_path)


def test_forward_defaults_to_disabled_without_a_fake_activation(tmp_path):
    path = _write_config(tmp_path, _valid_document())

    config = load_config(path, repo_root=tmp_path)

    assert config.forward.enabled is False
    assert config.forward.activation_date is None
    assert config.forward.model_version is None
    assert config.live_trading is False


def test_enabled_forward_requires_complete_frozen_contract(tmp_path):
    document = _valid_document()
    document["forward"] = {"enabled": True}
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="activation_date is required"):
        load_config(path, repo_root=tmp_path)


def test_enabled_forward_accepts_explicit_future_contract(tmp_path):
    document = _valid_document()
    document["forward"] = {
        "enabled": True,
        "activation_date": "2024-05-02",
        "model_name": "ridge",
        "model_version": "ridge-forward-v1",
        "training_asof": "2024-04-30",
        "parameter_candidate_index": 1,
        "timezone": "Asia/Shanghai",
        "market_close_hour": 15,
        "market_close_minute": 30,
        "require_clean_worktree": True,
    }
    path = _write_config(tmp_path, document)

    config = load_config(path, repo_root=tmp_path)

    assert config.forward.enabled is True
    assert config.forward.activation_date.isoformat() == "2024-05-02"
    assert config.forward.training_asof.isoformat() == "2024-04-30"


@pytest.mark.parametrize(
    "forward,match",
    [
        (
            {
                "enabled": True,
                "activation_date": "2024-05-02",
                "training_asof": "2024-04-30",
                "model_version": "../unsafe",
            },
            "safe non-empty identifier",
        ),
        (
            {"model_name": "ridge", "parameter_candidate_index": 99},
            "outside the configured ridge candidates",
        ),
        (
            {"model_name": "Ridge"},
            "canonical lowercase underscore form",
        ),
        (
            {"market_close_hour": 9, "market_close_minute": 0},
            "fixed at 15:30",
        ),
        (
            {
                "enabled": True,
                "activation_date": "2024-05-02",
                "training_asof": "2024-04-30",
                "model_version": "ridge-v1",
                "require_clean_worktree": False,
            },
            "must be true when enabled",
        ),
    ],
)
def test_forward_contract_rejects_unsafe_values(tmp_path, forward, match):
    document = _valid_document()
    document["forward"] = forward
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match=match):
        load_config(path, repo_root=tmp_path)


def test_enabled_factor_score_forward_requires_pre_registered_weights(tmp_path):
    document = _valid_document()
    document["forward"] = {
        "enabled": True,
        "activation_date": "2024-05-02",
        "training_asof": "2024-04-30",
        "model_name": "factor_score",
        "model_version": "factor-score-v1",
    }
    path = _write_config(tmp_path, document)

    with pytest.raises(ConfigurationError, match="requires fixed configured weights"):
        load_config(path, repo_root=tmp_path)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "fast"])
def test_cache_lock_timeout_rejects_unsafe_values(tmp_path, value):
    document = _valid_document()
    document["cache"] = {"lock_timeout_seconds": value}
    path = _write_config(tmp_path, document)
    with pytest.raises(ConfigurationError, match="lock_timeout_seconds"):
        load_config(path, repo_root=tmp_path)


def test_cache_lock_timeout_can_be_configured(tmp_path):
    document = _valid_document()
    document["cache"] = {"lock_timeout_seconds": 0.25}
    config = load_config(_write_config(tmp_path, document), repo_root=tmp_path)
    assert config.cache.lock_timeout_seconds == 0.25


def test_inference_defaults_are_frozen_in_serialized_config(tmp_path):
    config = load_config(_write_config(tmp_path, _valid_document()), repo_root=tmp_path)
    assert config.inference.enabled is True
    assert config.inference.hac_lags == 20
    assert config.inference.bootstrap_repetitions == 2000
    assert config.to_dict()["inference"]["min_coverage"] == .8


@pytest.mark.parametrize("inference,match", [
    ({"enabled": "true"}, "enabled"), ({"enabled": 1}, "enabled"),
    ({"hac_lags": True}, "hac_lags"), ({"hac_lags": 3}, "overlapping"),
    ({"bootstrap_block_days": 4}, "holding_days"),
    ({"bootstrap_repetitions": 50}, "repetitions"),
    ({"min_valid_days": 10}, "two blocks"),
    ({"confidence_level": 1.}, "probabilities"),
    ({"min_coverage": float("nan")}, "finite"),
    ({"baseline": "ridge"}, "unknown field"),
    ({"primary_metric": "net_alpha"}, "unknown field"),
])
def test_inference_protocol_rejects_unsafe_or_test_selected_choices(tmp_path, inference, match):
    document = _valid_document()
    document["inference"] = inference
    with pytest.raises(ConfigurationError, match=match):
        load_config(_write_config(tmp_path, document), repo_root=tmp_path)


def test_inference_can_be_explicitly_disabled_but_invalid_parameters_still_fail(tmp_path):
    document = _valid_document()
    document["inference"] = {"enabled": False}
    config = load_config(_write_config(tmp_path, document), repo_root=tmp_path)
    assert config.inference.enabled is False


def test_holding_period_and_inference_horizon_must_agree(tmp_path):
    document = _valid_document()
    document["label"] = {"holding_days": 21}
    with pytest.raises(ConfigurationError, match="bootstrap_block_days"):
        load_config(_write_config(tmp_path, document), repo_root=tmp_path)
