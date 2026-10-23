"""Daily cross-sectional winsorisation and standardisation."""

from typing import Iterable, List

import numpy as np
import pandas as pd

from qbot.research.errors import ConfigurationError, SchemaError


def _mad_zscore(
    values: pd.Series, mad_multiplier: float, minimum_count: int
) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    finite = numeric.dropna()
    if len(finite) < minimum_count:
        return pd.Series(np.nan, index=values.index, dtype=float)
    median = finite.median()
    mad = (finite - median).abs().median()
    clipped = numeric.copy()
    if pd.notna(mad) and mad > 0:
        bound = mad_multiplier * 1.4826 * mad
        clipped = clipped.clip(lower=median - bound, upper=median + bound)
    mean = clipped.mean(skipna=True)
    standard_deviation = clipped.std(skipna=True, ddof=0)
    if pd.isna(standard_deviation):
        return pd.Series(np.nan, index=values.index, dtype=float)
    if standard_deviation == 0:
        result = pd.Series(np.nan, index=values.index, dtype=float)
        result.loc[clipped.notna()] = 0.0
        return result
    return (clipped - mean) / standard_deviation


def preprocess_cross_sections(
    frame: pd.DataFrame,
    factor_names: Iterable[str],
    method: str = "mad",
    mad_multiplier: float = 5.0,
    lower_quantile: float = 0.01,
    upper_quantile: float = 0.99,
    minimum_count: int = 20,
    raw_prefix: str = "raw_",
) -> pd.DataFrame:
    """Preprocess each date independently, preserving raw factor columns."""

    names: List[str] = list(factor_names)
    missing = set(["date"] + names).difference(frame.columns)
    if missing:
        raise SchemaError("Preprocess input missing columns %s" % sorted(missing))
    if method not in {"mad", "quantile"}:
        raise ConfigurationError("Winsorization method must be mad or quantile")
    if minimum_count < 1:
        raise ConfigurationError("minimum_count must be positive")
    if not 0 <= lower_quantile < upper_quantile <= 1:
        raise ConfigurationError("Invalid winsorization quantiles")
    result = frame.copy()
    for name in names:
        result[raw_prefix + name] = result[name]
        if method == "mad":
            result[name] = result.groupby("date", sort=False, group_keys=False)[name].transform(
                lambda values: _mad_zscore(values, mad_multiplier, minimum_count)
            )
        else:
            def quantile_zscore(values: pd.Series) -> pd.Series:
                numeric = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
                if numeric.notna().sum() < minimum_count:
                    return pd.Series(np.nan, index=values.index, dtype=float)
                lower = numeric.quantile(lower_quantile)
                upper = numeric.quantile(upper_quantile)
                clipped = numeric.clip(lower=lower, upper=upper)
                deviation = clipped.std(skipna=True, ddof=0)
                if deviation == 0:
                    output = pd.Series(np.nan, index=values.index, dtype=float)
                    output.loc[clipped.notna()] = 0.0
                    return output
                return (clipped - clipped.mean(skipna=True)) / deviation

            result[name] = result.groupby("date", sort=False, group_keys=False)[name].transform(
                quantile_zscore
            )
    return result
