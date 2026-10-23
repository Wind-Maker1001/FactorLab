"""Simple, reproducible portfolio weighting schemes."""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from qbot.research.errors import ConfigurationError


def equal_weights(symbols: Iterable[str]) -> pd.Series:
    """Return equal weights indexed by symbol.

    Duplicate symbols are rejected instead of silently receiving multiple
    allocations.  An empty input produces an empty, floating-point series.
    """

    values = list(symbols)
    if len(values) != len(set(values)):
        raise ConfigurationError("equal_weights requires unique symbols")
    if not values:
        return pd.Series(dtype=float, name="target_weight")
    weight = 1.0 / float(len(values))
    return pd.Series(weight, index=pd.Index(values, name="symbol"), name="target_weight")


__all__ = ["equal_weights"]
