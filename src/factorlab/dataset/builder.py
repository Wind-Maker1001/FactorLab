"""Point-in-time, leakage-safe dataset assembly."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.factors.preprocess import preprocess_cross_sections
from factorlab.factors.registry import compute_factors
from factorlab.labels.forward_return import build_forward_excess_return_labels
from factorlab.market.regime import (
    MARKET_FEATURE_COLUMNS,
    build_market_regime_features,
)
from factorlab.universe.csi500 import validate_daily_membership
from factorlab.universe.filters import (
    UniverseFilterConfig,
    apply_universe_filters,
    build_filter_flags,
)


@dataclass(frozen=True)
class DatasetBuildOptions:
    factor_names: Sequence[str]
    holding_days: int = 5
    entry_price: str = "next_open"
    expected_member_count: int = 500
    winsorization: str = "mad"
    mad_multiplier: float = 5.0
    lower_quantile: float = 0.01
    upper_quantile: float = 0.99
    minimum_cross_section: int = 20
    include_market_regime: bool = True
    filter_config: UniverseFilterConfig = UniverseFilterConfig()


def _calendar(values: Iterable[object]) -> pd.DatetimeIndex:
    dates = pd.DatetimeIndex(pd.to_datetime(list(values), errors="raise"))
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    dates = dates.normalize()
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise SchemaError("Dataset calendar must be unique and sorted")
    return dates


def _full_bar_grid(
    bars: pd.DataFrame, dates: pd.DatetimeIndex, symbols: Sequence[str]
) -> pd.DataFrame:
    working = bars.copy()
    working["date"] = pd.to_datetime(working["date"], errors="raise").dt.tz_localize(None).dt.normalize()
    if working.duplicated(["date", "symbol"]).any():
        raise SchemaError("Bars contain duplicate (date, symbol) rows")
    grid = pd.MultiIndex.from_product([dates, sorted(set(symbols))], names=["date", "symbol"])
    result = working.set_index(["date", "symbol"]).reindex(grid).reset_index()
    return result.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)


class DatasetBuilder:
    """Compose strict PIT membership, factors, labels, and filters."""

    def __init__(self, options: DatasetBuildOptions) -> None:
        self.options = options
        if not options.factor_names:
            raise ConfigurationError("At least one factor is required")

    def build(
        self,
        bars: pd.DataFrame,
        benchmark: pd.DataFrame,
        membership: pd.DataFrame,
        status: pd.DataFrame,
        listings: pd.DataFrame,
        calendar: Iterable[object],
    ) -> pd.DataFrame:
        dates = _calendar(calendar)
        supplied_membership_dates = pd.DatetimeIndex(
            pd.to_datetime(membership["date"], errors="raise").drop_duplicates().sort_values()
        ).normalize()
        if supplied_membership_dates.empty:
            raise SchemaError("PIT membership calendar must not be empty")
        if len(supplied_membership_dates.difference(dates)):
            raise SchemaError("PIT membership dates must be contained in the full calendar")
        # Membership is intentionally stored only for the research interval,
        # while bars/status include pre-start sessions needed for filters and
        # factor lookbacks.  Validate every full-calendar date between the
        # supplied membership endpoints so an interior PIT gap still fails.
        research_dates = dates[
            (dates >= supplied_membership_dates.min())
            & (dates <= supplied_membership_dates.max())
        ]
        members = validate_daily_membership(
            membership,
            requested_dates=research_dates,
            expected_member_count=self.options.expected_member_count,
        )
        symbols = members["symbol"].drop_duplicates().tolist()
        full_bars = _full_bar_grid(bars, dates, symbols)
        flags = build_filter_flags(
            full_bars, status, listings, dates, self.options.filter_config
        )
        eligible = apply_universe_filters(
            members, flags, research_dates, self.options.expected_member_count
        )
        if eligible.empty:
            raise SchemaError(
                "Universe filters produced no eligible stock-days; verify the "
                "complete status/listing bundle and filter thresholds"
            )
        factor_frame = compute_factors(full_bars, self.options.factor_names)
        selected = eligible[["date", "symbol"]].merge(
            factor_frame,
            on=["date", "symbol"],
            how="left",
            validate="one_to_one",
        )
        labels = build_forward_excess_return_labels(
            full_bars,
            benchmark,
            holding_days=self.options.holding_days,
            entry_price=self.options.entry_price,
            calendar=dates,
        )
        selected = selected.merge(
            labels, on=["date", "symbol"], how="left", validate="one_to_one"
        )
        if self.options.include_market_regime:
            market = build_market_regime_features(benchmark)
            market_dates = pd.DatetimeIndex(market["date"])
            missing_market_dates = research_dates.difference(market_dates)
            if len(missing_market_dates):
                raise SchemaError(
                    "Benchmark market-regime features are missing research dates: %s"
                    % ", ".join(
                        value.date().isoformat()
                        for value in missing_market_dates[:5]
                    )
                )
            selected = selected.merge(
                market.loc[:, list(MARKET_FEATURE_COLUMNS)],
                on="date",
                how="left",
                validate="many_to_one",
            )
        selected = preprocess_cross_sections(
            selected,
            self.options.factor_names,
            method=self.options.winsorization,
            mad_multiplier=self.options.mad_multiplier,
            lower_quantile=self.options.lower_quantile,
            upper_quantile=self.options.upper_quantile,
            minimum_count=self.options.minimum_cross_section,
        )
        if selected.duplicated(["date", "symbol"]).any():
            raise SchemaError("Built dataset contains duplicate primary keys")
        if selected.empty:
            raise SchemaError("Built dataset is empty")
        return selected.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)

    def write_artifacts(
        self,
        dataset: pd.DataFrame,
        processed_path: Path,
        features_dir: Path,
    ) -> None:
        """Atomically persist raw factors, processed factors, and full dataset."""

        destination = Path(processed_path)
        feature_destination = Path(features_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        feature_destination.mkdir(parents=True, exist_ok=True)
        raw_names = ["raw_" + name for name in self.options.factor_names]
        keys = ["date", "symbol"]
        self._atomic_write(dataset[keys + raw_names], feature_destination / "raw_factors.parquet")
        self._atomic_write(
            dataset[keys + list(self.options.factor_names)],
            feature_destination / "processed_factors.parquet",
        )
        self._atomic_write(dataset, destination)

    @staticmethod
    def _atomic_write(frame: pd.DataFrame, destination: Path) -> None:
        temporary = destination.with_name(destination.name + ".tmp")
        frame.to_parquet(temporary, index=False)
        os.replace(str(temporary), str(destination))
