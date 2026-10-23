"""Canonical forward-research contract independent of the rolling data end."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from typing import Any, Dict

from factorlab.config import ResearchConfig
from factorlab.errors import ConfigurationError


def selected_forward_model_parameters(config: ResearchConfig) -> Dict[str, Any]:
    """Return the one explicitly pre-registered final-model specification."""

    name = config.forward.model_name.strip().lower().replace("-", "_")
    index = config.forward.parameter_candidate_index
    if name == "factor_score":
        return {
            "weights": dict(config.models.factor_score.weights),
            "normalize_by_absolute_weight": (
                config.models.factor_score.normalize_by_absolute_weight
            ),
        }
    if name == "ridge":
        return {
            "alpha": float(config.models.ridge.alphas[index]),
            "fit_intercept": config.models.ridge.fit_intercept,
            "max_iter": config.models.ridge.max_iter,
            "tolerance": config.models.ridge.tolerance,
        }
    if name == "lightgbm":
        return {
            **asdict(config.models.lightgbm.candidates[index]),
            "n_jobs": config.models.lightgbm.n_jobs,
        }
    if name == "xgboost":
        return {
            **asdict(config.models.xgboost.candidates[index]),
            # XGBoostResearchModel exposes gamma as a frozen constructor
            # parameter even though the deliberately small candidate grid
            # does not tune it in Phase 3.
            "gamma": 0.0,
            "n_jobs": config.models.xgboost.n_jobs,
        }
    raise ConfigurationError("unsupported forward model: %s" % name)


def forward_contract_payload(config: ResearchConfig) -> Dict[str, Any]:
    """Build the frozen signal contract.

    ``data.end`` is intentionally excluded because an honest daily workflow
    advances it to the signal date.  The historical start, training cutoff,
    feature/preprocessing definitions, model specification, portfolio rule,
    and activation controls remain frozen.
    """

    snapshot = config.to_dict()
    return {
        "schema_version": 2,
        "project": config.project.name,
        "seed": config.seed,
        "live_trading": config.live_trading,
        "data": {
            "start": config.data.start.isoformat(),
            "provider": config.data.provider,
            "benchmark": config.data.benchmark,
            "adjustment": config.data.adjustment,
            "calendar_source": config.data.calendar_source,
            "warmup_trading_days": config.data.warmup_trading_days,
        },
        "universe": snapshot["universe"],
        "label": snapshot["label"],
        "factors": list(config.factors),
        "preprocessing": snapshot["preprocessing"],
        "market_regime": snapshot["market_regime"],
        "evaluation": snapshot["evaluation"],
        "validation": snapshot["validation"],
        "portfolio": snapshot["portfolio"],
        "backtest": snapshot["backtest"],
        "fees": snapshot["fees"],
        "forward": {
            "enabled": config.forward.enabled,
            "activation_date": (
                None
                if config.forward.activation_date is None
                else config.forward.activation_date.isoformat()
            ),
            "model_name": config.forward.model_name,
            "model_version": config.forward.model_version,
            "training_asof": (
                None
                if config.forward.training_asof is None
                else config.forward.training_asof.isoformat()
            ),
            "parameter_candidate_index": (
                config.forward.parameter_candidate_index
            ),
            "timezone": config.forward.timezone,
            "market_close_hour": config.forward.market_close_hour,
            "market_close_minute": config.forward.market_close_minute,
            "require_clean_worktree": config.forward.require_clean_worktree,
            "evaluation_version_policy": config.forward.evaluation_version_policy,
        },
        "model_parameters": selected_forward_model_parameters(config),
    }


def forward_contract_json(config: ResearchConfig) -> str:
    return json.dumps(
        forward_contract_payload(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def forward_contract_sha256(config: ResearchConfig) -> str:
    return hashlib.sha256(forward_contract_json(config).encode("utf-8")).hexdigest()


__all__ = [
    "forward_contract_json",
    "forward_contract_payload",
    "forward_contract_sha256",
    "selected_forward_model_parameters",
]
