"""Causal bar scoring with an explicitly bound, immutable forward model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from math import isfinite
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.forward.model_bundle import FrozenModelBundle, load_model_bundle
from factorlab.models.factory import DEFAULT_CATALOG
from factorlab.trading.contracts import Market
from factorlab.trading.strategy import ClosedBar, StrategySignal


@dataclass(frozen=True)
class FrozenFeature:
    column: str
    kind: str
    lookback: int = 1

    def __post_init__(self) -> None:
        if not self.column or self.column != self.column.strip():
            raise ValueError("feature column must be canonical and non-empty")
        if self.kind not in {"return", "mean_return", "volatility", "open", "high", "low", "close", "volume"}:
            raise ValueError("unsupported fixed feature kind")
        if type(self.lookback) is not int or self.lookback < 1:
            raise ValueError("lookback must be a positive integer")
        if self.kind in {"open", "high", "low", "close", "volume"} and self.lookback != 1:
            raise ValueError("bar value features require lookback=1")

    @property
    def required_bars(self) -> int:
        return self.lookback + 1 if self.kind in {"return", "mean_return", "volatility"} else 1

    def value(self, bars: list[ClosedBar]) -> float:
        if self.kind in {"open", "high", "low", "close", "volume"}:
            return float(getattr(bars[-1], self.kind))
        closes = np.array([float(bar.close) for bar in bars[-self.required_bars:]])
        if self.kind == "return":
            return float(closes[-1] / closes[0] - 1)
        returns = closes[1:] / closes[:-1] - 1
        return float(returns.mean() if self.kind == "mean_return" else returns.std(ddof=0))


def deployment_contract_sha256(
    market: str,
    symbol: str,
    interval_seconds: int,
    source: str,
    features: tuple[FrozenFeature, ...],
) -> str:
    """Digest the deployment contract to record when freezing a training bundle."""
    contract = {
        "feature_algorithm_version": "causal-bars-v1",
        "dtype": "float64",
        "market": market,
        "symbol": symbol,
        "interval_seconds": interval_seconds,
        "source": source,
        "features": [dict(column=f.column, kind=f.kind, lookback=f.lookback) for f in features],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrozenDeploymentBinding:
    market: str
    symbol: str
    interval_seconds: int
    source: str
    source_sha256: str
    research_contract_sha256: str
    features: tuple[FrozenFeature, ...]

    def __post_init__(self) -> None:
        for value in (self.market, self.symbol, self.source):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("market, symbol and source must be canonical non-empty strings")
        if type(self.interval_seconds) is not int or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        for value in (self.source_sha256, self.research_contract_sha256):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("binding hashes must be canonical SHA-256 digests")
        features = tuple(self.features)
        if not features or any(type(feature) is not FrozenFeature for feature in features):
            raise ValueError("features must be ordered FrozenFeature definitions")
        if len({feature.column for feature in features}) != len(features):
            raise ValueError("duplicate feature columns")
        object.__setattr__(self, "features", features)
        digest = deployment_contract_sha256(
            self.market, self.symbol, self.interval_seconds, self.source, features,
        )
        if self.research_contract_sha256 != digest:
            raise ValueError("deployment contract digest mismatch")
        if self.market != Market.BINANCE_USDM.value or self.symbol != "NVDAUSDT":
            raise ValueError("unsupported deployment market or symbol")


@dataclass(frozen=True)
class FixedScoreConversion:
    long_threshold: float
    exit_threshold: float
    target_weight: Decimal

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isfinite(value) for value in (self.long_threshold, self.exit_threshold)):
            raise ValueError("score thresholds must be finite")
        if self.exit_threshold >= self.long_threshold:
            raise ValueError("exit_threshold must be below long_threshold")
        if not isinstance(self.target_weight, Decimal) or not self.target_weight.is_finite() or not 0 < self.target_weight <= 1:
            raise ValueError("target_weight must be a Decimal in (0, 1]")

    def convert(self, score: float) -> tuple[str, Decimal | None]:
        if score > self.long_threshold:
            return "BUY", self.target_weight
        if score < self.exit_threshold:
            return "SELL", Decimal(0)
        return "HOLD", None


class FrozenModelStrategy:
    """Trusted bundles only; deployment definitions and conversion are caller-frozen.

    The bundle's research contract digest binds the ordered feature definitions
    and market deployment; its source digest identifies the training snapshot.
    """

    def __init__(
        self,
        bundle: FrozenModelBundle | str | Path,
        *,
        strategy_id: str,
        strategy_version: str,
        expected_model_id: str,
        expected_model_version: str,
        expected_bundle_id: str,
        binding: FrozenDeploymentBinding,
        conversion: FixedScoreConversion,
    ) -> None:
        for value in (strategy_id, strategy_version):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("strategy identity must be canonical and non-empty")
        # Registry resolution rejects aliases and unregistered deployment identities.
        registration = DEFAULT_CATALOG.resolve(expected_model_id, "legacy-v1")
        if registration.model_id != expected_model_id:
            raise ValueError("expected_model_id must be canonical")
        if not isinstance(bundle, FrozenModelBundle):
            bundle = load_model_bundle(
                bundle, expected_model_name=expected_model_id,
                expected_feature_names=tuple(feature.column for feature in binding.features),
                expected_source_sha256=binding.source_sha256,
            )
        expected = {
            "model_name": expected_model_id,
            "model_version": expected_model_version,
            "content_sha256": expected_bundle_id,
            "source_sha256": binding.source_sha256,
            "research_contract_sha256": binding.research_contract_sha256,
        }
        for key, value in expected.items():
            if bundle.metadata[key] != value:
                raise ValueError("bundle deployment mismatch: " + key)
        if bundle.feature_names != tuple(feature.column for feature in binding.features):
            raise ValueError("bundle deployment mismatch: feature order")
        self._bundle = bundle
        self._strategy_id = strategy_id
        self._strategy_version = strategy_version
        self._binding = binding
        self._conversion = conversion
        self._identity = dict(expected)
        self.history_limit = max(feature.required_bars for feature in binding.features)

    def describe(self) -> dict:
        return {
            "strategy_id": self._strategy_id,
            "strategy_version": self._strategy_version,
            "model_id": self._identity["model_name"],
            "model_version": self._identity["model_version"],
            "model_bundle_id": self._identity["content_sha256"],
            "market": self._binding.market,
            "symbol": self._binding.symbol,
            "source": self._binding.source,
            "interval_seconds": self._binding.interval_seconds,
            "source_sha256": self._binding.source_sha256,
            "research_contract_sha256": self._binding.research_contract_sha256,
            "features": [dict(column=f.column, kind=f.kind, lookback=f.lookback) for f in self._binding.features],
            "conversion": {"long_threshold": self._conversion.long_threshold,
                           "exit_threshold": self._conversion.exit_threshold,
                           "target_weight": str(self._conversion.target_weight)},
        }

    def signal(self, bars: list[ClosedBar], now: datetime) -> StrategySignal:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not bars:
            raise ValueError("at least one closed bar is required")
        previous = None
        for bar in bars:
            if (bar.symbol != self._binding.symbol or bar.source != self._binding.source
                    or bar.interval_seconds != self._binding.interval_seconds):
                raise ValueError("bar deployment mismatch")
            if not bar.complete or bar.closed_at > now:
                raise ValueError("only complete, already closed bars may be scored")
            if previous is not None and bar.opened_at != previous.closed_at:
                raise ValueError("bars must be ordered and contiguous")
            previous = bar
        score = None
        direction, weight, status, reason = "HOLD", None, "INSUFFICIENT_DATA", "feature_warmup"
        if len(bars) >= self.history_limit:
            # Training labels are admitted through the entire as-of calendar day.
            if bars[-1].closed_at.astimezone(timezone.utc).date() <= date.fromisoformat(
                self._bundle.metadata["training_asof"]
            ):
                raise ValueError("input watermark must be after the training_asof day")
            frame = pd.DataFrame([[feature.value(bars) for feature in self._binding.features]],
                                 columns=[feature.column for feature in self._binding.features])
            if not np.isfinite(frame.to_numpy()).all():
                raise ValueError("non-finite causal features")
            score = float(self._bundle.predict(frame).iloc[0])
            if not isfinite(score):
                raise ValueError("model produced a non-finite score")
            direction, weight = self._conversion.convert(score)
            status, reason = ("NO_SIGNAL" if direction == "HOLD" else "SIGNAL"), None
        identity = {"deployment": self.describe(), "watermark": bars[-1].closed_at.isoformat(),
                    "score": score, "direction": direction, "status": status}
        signal_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False).encode()).hexdigest()
        return StrategySignal(
            strategy_id=self._strategy_id, strategy_version=self._strategy_version,
            symbol=self._binding.symbol, generated_at=now, input_watermark=bars[-1].closed_at,
            direction=direction, target_weight=weight, source=self._binding.source,
            signal_id=signal_id, status=status, model_id=self._identity["model_name"],
            model_version=self._identity["model_version"], model_bundle_id=self._identity["content_sha256"],
            score=score, reason=reason,
        )
