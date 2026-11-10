"""Train-only Engle–Granger selection and causal spread statistics.

OLS orientation follows column order: y = intercept + hedge_ratio * x.
Completeness uses the training calendar; overlap is joint / union observations.
"""
from dataclasses import asdict, dataclass
from itertools import combinations, islice

import numpy as np
import pandas as pd


class _StatisticalRejection(ValueError):
    def __init__(self, reason, statistics):
        super().__init__(reason)
        self.statistics = statistics


@dataclass(frozen=True)
class SelectionConfig:
    train_end: str
    train_start: str | None = None
    min_observations: int = 120
    min_completeness: float = 0.95
    min_overlap: float = 0.9
    price_transform: str = "raw"
    coint_pvalue: float = 0.05
    adf_pvalue: float = 0.05
    max_hedge_ratio_drift: float = 0.5
    max_candidates: int = 100
    max_pairs: int = 10

    def __post_init__(self):
        for name in ("max_candidates", "max_pairs"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(name + " must be a positive integer")
        if self.price_transform not in ("raw", "log"):
            raise ValueError("price_transform must be raw or log")
        if isinstance(self.min_observations, bool) or not isinstance(self.min_observations, int) or self.min_observations < 20:
            raise ValueError("min_observations must be an integer >= 20")
        for name in ("min_completeness", "min_overlap", "coint_pvalue", "adf_pvalue"):
            value = getattr(self, name)
            if not np.isfinite(value) or not 0 < value <= 1:
                raise ValueError(name + " must be in (0, 1]")
        if not np.isfinite(self.max_hedge_ratio_drift) or self.max_hedge_ratio_drift < 0:
            raise ValueError("max_hedge_ratio_drift must be finite and nonnegative")
        if self.train_end is None:
            raise ValueError("train_end is required")
        end = pd.Timestamp(self.train_end)
        start = pd.Timestamp(self.train_start) if self.train_start is not None else None
        if pd.isna(end) or end.tzinfo is not None or (start is not None and (pd.isna(start) or start.tzinfo is not None or start > end)):
            raise ValueError("training boundaries must be ordered timezone-free dates")


@dataclass(frozen=True)
class PairModel:
    y_symbol: str
    x_symbol: str
    price_transform: str
    intercept: float
    hedge_ratio: float
    spread_mean: float
    spread_std: float
    n_observations: int
    train_start: str
    train_end: str
    coint_statistic: float
    coint_pvalue: float
    adf_statistic: float
    adf_pvalue: float
    hedge_ratio_first_half: float
    hedge_ratio_second_half: float
    hedge_ratio_drift: float

    def spread(self, prices: pd.DataFrame) -> pd.Series:
        values = prices.loc[:, [self.y_symbol, self.x_symbol]].astype(float)
        if self.price_transform == "log":
            if (values <= 0).any().any():
                raise ValueError("log prices must be positive")
            values = np.log(values)
        return (values[self.y_symbol] - self.intercept - self.hedge_ratio * values[self.x_symbol]).rename("spread")

    def zscore(self, prices: pd.DataFrame, window: int) -> pd.Series:
        """Score today's spread against the preceding window, excluding today."""
        if isinstance(window, bool) or not isinstance(window, int) or window < 2:
            raise ValueError("window must be an integer >= 2")
        spread = self.spread(prices)
        history = spread.shift(1).rolling(window, min_periods=window)
        std = history.std(ddof=1)
        return ((spread - history.mean()) / std.where(std > 0)).rename("zscore")


def _training(prices, config):
    if not isinstance(prices.index, pd.DatetimeIndex) or prices.index.hasnans or prices.index.has_duplicates or not prices.index.is_monotonic_increasing or prices.index.tz is not None:
        raise ValueError("prices require unique ordered timezone-free DatetimeIndex")
    if prices.columns.has_duplicates or any(not isinstance(c, str) for c in prices.columns):
        raise ValueError("price columns must be unique string symbols")
    train = prices.loc[:pd.Timestamp(config.train_end)]
    if config.train_start is not None:
        train = train.loc[pd.Timestamp(config.train_start):]
    train = train.astype(float)
    if np.isinf(train.to_numpy()).any():
        raise ValueError("prices must be finite or missing")
    return train


def _ols(values):
    y, x = values[:, 0], values[:, 1]
    if np.ptp(x) == 0 or np.ptp(y) == 0:
        raise ValueError("constant training series")
    intercept, hedge = np.linalg.lstsq(np.column_stack((np.ones(len(x)), x)), y, rcond=None)[0]
    return float(intercept), float(hedge)


def fit_pair(prices: pd.DataFrame, config: SelectionConfig) -> PairModel:
    """Fit and validate one ordered pair; rejected candidates raise ValueError."""
    try:
        from statsmodels.tsa.stattools import adfuller, coint
    except ModuleNotFoundError as exc:
        if exc.name != "statsmodels":
            raise
        raise ModuleNotFoundError(
            'install factorlab[pairs] for cointegration research: pip install "factorlab[pairs]"',
            name="statsmodels",
        ) from exc

    if prices.shape[1] != 2:
        raise ValueError("fit_pair requires exactly two price columns")
    train = _training(prices, config)
    present = train.notna()
    if len(train) == 0 or (present.mean() < config.min_completeness).any():
        raise ValueError("insufficient completeness")
    joint = present.all(axis=1)
    if joint.sum() < config.min_observations:
        raise ValueError("insufficient observations")
    if joint.sum() / present.any(axis=1).sum() < config.min_overlap:
        raise ValueError("insufficient overlap")
    paired = train.loc[joint]
    values = paired.to_numpy()
    if (values <= 0).any():
        raise ValueError("prices must be positive")
    if config.price_transform == "log":
        if (values <= 0).any():
            raise ValueError("log prices must be positive")
        values = np.log(values)
    intercept, hedge = _ols(values)
    residual = values[:, 0] - intercept - hedge * values[:, 1]
    spread_std = float(np.std(residual, ddof=1))
    if spread_std <= np.finfo(float).eps * max(1.0, float(np.max(np.abs(values)))):
        raise ValueError("degenerate spread")
    split = len(values) // 2
    _, first = _ols(values[:split])
    _, second = _ols(values[split:])
    drift = abs(second - first) / abs(hedge) if hedge else float("inf")
    cstat, cpvalue, _ = coint(values[:, 0], values[:, 1], trend="c", autolag="aic")
    astat, apvalue, *_ = adfuller(residual, regression="n", autolag="AIC")
    statistics = dict(coint_statistic=float(cstat), coint_pvalue=float(cpvalue),
                      adf_statistic=float(astat), adf_pvalue=float(apvalue),
                      intercept=intercept, hedge_ratio=hedge, spread_std=spread_std,
                      spread_mean=float(np.mean(residual)), hedge_ratio_drift=float(drift),
                      hedge_ratio_first_half=first, hedge_ratio_second_half=second,
                      n_observations=len(values), train_start=paired.index[0].isoformat(),
                      train_end=paired.index[-1].isoformat(), price_transform=config.price_transform)
    if hedge <= 0:
        raise _StatisticalRejection("nonpositive hedge ratio", statistics)
    if first <= 0 or second <= 0 or drift > config.max_hedge_ratio_drift:
        raise _StatisticalRejection("unstable hedge ratio", statistics)
    # Residual ADF is diagnostic; cointegration uses Engle–Granger critical values.
    if not np.isfinite(cstat) or cpvalue > config.coint_pvalue:
        raise _StatisticalRejection("Engle-Granger rejects cointegration", statistics)
    if apvalue > config.adf_pvalue:
        raise _StatisticalRejection("residual ADF rejects stationarity", statistics)
    return PairModel(
        str(prices.columns[0]), str(prices.columns[1]), config.price_transform,
        intercept, hedge, float(np.mean(residual)), spread_std, len(values),
        paired.index[0].isoformat(), paired.index[-1].isoformat(),
        float(cstat), float(cpvalue), float(astat), float(apvalue), first, second, float(drift),
    )


def select_pairs(prices: pd.DataFrame, config: SelectionConfig) -> tuple[list[PairModel], pd.DataFrame]:
    """Return accepted models and a row per candidate, including rejection reasons.

    Diagnostic attrs count all pairs and pairs meeting joint minimum length.
    """
    train = _training(prices, config)
    models, rows = [], []
    eligible = 0
    for y, x in islice(combinations(sorted(prices.columns), 2), config.max_candidates):
        n = int(train.loc[:, [y, x]].notna().all(axis=1).sum())
        eligible += n >= config.min_observations
        row = {"y_symbol": y, "x_symbol": x, "n_observations": n,
               "price_correlation": float(train[y].corr(train[x]))}
        try:
            model = fit_pair(prices.loc[:, [y, x]], config)
        except _StatisticalRejection as exc:
            row.update(exc.statistics, accepted=False, reason=str(exc))
        except ValueError as exc:
            row.update(accepted=False, reason=str(exc))
        else:
            models.append(model)
            row.update(asdict(model), accepted=True, reason="")
        rows.append(row)
    diagnostics = pd.DataFrame(rows, columns=list(dict.fromkeys(
        ["y_symbol", "x_symbol", "n_observations", "price_correlation", "accepted", "reason"] + list(PairModel.__dataclass_fields__)
    )))
    models = sorted(models, key=lambda model: (model.coint_pvalue, model.y_symbol, model.x_symbol))[:config.max_pairs]
    diagnostics.attrs.update(candidate_count=len(rows), eligible_candidate_count=int(eligible))
    return models, diagnostics
