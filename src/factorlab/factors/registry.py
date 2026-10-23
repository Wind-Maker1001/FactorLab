"""Explicit factor registry used by YAML-driven dataset construction."""

from typing import Dict, Iterable

import pandas as pd

from factorlab.errors import ConfigurationError

from .base import Factor
from .liquidity import AmihudFactor, TurnoverFactor, VolumeRatioFactor
from .momentum import MomentumFactor
from .reversal import ReversalFactor
from .trend import TrendFactor
from .volatility import VolatilityFactor


FACTOR_REGISTRY: Dict[str, Factor] = {
    "REV1": ReversalFactor(1),
    "REV5": ReversalFactor(5),
    "MOM20": MomentumFactor(20),
    "MOM60": MomentumFactor(60),
    "VOL5": VolatilityFactor(5),
    "VOL20": VolatilityFactor(20),
    "VOL60": VolatilityFactor(60),
    "TURN5": TurnoverFactor(5),
    "TURN20": TurnoverFactor(20),
    "VOLUME_RATIO": VolumeRatioFactor(),
    "AMIHU20": AmihudFactor(),
    "PRICE_MA20": TrendFactor("PRICE_MA20"),
    "MA5_MA20": TrendFactor("MA5_MA20"),
    "MA20_MA60": TrendFactor("MA20_MA60"),
}


def get_factor(name: str) -> Factor:
    try:
        return FACTOR_REGISTRY[str(name).upper()]
    except KeyError as exc:
        raise ConfigurationError(
            "Unknown factor %r; available factors: %s"
            % (name, ", ".join(sorted(FACTOR_REGISTRY)))
        ) from exc


def compute_factors(bars: pd.DataFrame, factor_names: Iterable[str]) -> pd.DataFrame:
    """Compute selected registered factors without row-wise DataFrame loops."""

    names = [str(name).upper() for name in factor_names]
    if len(names) != len(set(names)):
        raise ConfigurationError("Factor list contains duplicates")
    result = bars.copy()
    for name in names:
        result[name] = get_factor(name).compute(result)
    return result.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
