"""Point-in-time, leakage-safe dataset assembly."""

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from factorlab.errors import ConfigurationError, SchemaError
from factorlab.factors.preprocess import preprocess_cross_sections
from factorlab.factors.registry import compute_factors
from factorlab.factors.registry_v2 import FactorDataContext, FactorRef, FactorRegistrySnapshot
from factorlab.dataset.identity import (
    IDENTITY_ATTR, binding, check_identity, file_hash, frame_hash, seal,
    verify_dataset_artifacts,
)
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
    factor_names: Sequence[str] = ()
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

    def __init__(self, options: DatasetBuildOptions, *,
                 snapshot: FactorRegistrySnapshot | None = None,
                 factor_refs: Sequence[FactorRef] | None = None,
                 data_context: FactorDataContext | None = None,
                 data_version: str | None = None,
                 calendar_id: str | None = None) -> None:
        self.options = options
        self.snapshot = snapshot
        self.factor_refs = None
        self.data_context = data_context
        self.data_version = data_version
        self.calendar_id = calendar_id
        self.factor_names = tuple(options.factor_names)
        if snapshot is None:
            if any(value is not None for value in
                   (factor_refs, data_context, data_version, calendar_id)):
                raise ConfigurationError("V2 parameters require a frozen snapshot")
        else:
            if type(snapshot) is not FactorRegistrySnapshot:
                raise ConfigurationError("A frozen FactorRegistrySnapshot is required")
            if factor_refs is None or isinstance(factor_refs, (str, bytes)):
                raise ConfigurationError("Explicit versioned FactorRefs required")
            try:
                self.factor_refs = tuple(factor_refs)
            except TypeError as exc:
                raise ConfigurationError("Explicit versioned FactorRefs required") from exc
            snapshot.resolve(self.factor_refs)
            if not self.factor_refs:
                raise ConfigurationError("At least one FactorRef is required")
            if type(data_context) is not FactorDataContext or data_context != FactorDataContext(
                    data_context.data_source, data_context.availability_evidence):
                raise ConfigurationError("Unmodified FactorDataContext required")
            if any(type(value) is not str or not value.strip()
                   for value in (data_version, calendar_id)):
                raise ConfigurationError("Explicit data_version and calendar_id required")
            names = tuple(ref.factor_id for ref in self.factor_refs)
            if self.factor_names and self.factor_names != names:
                raise ConfigurationError("factor_names must match ordered FactorRefs")
            protected = {"date", "symbol", "entry_date", "exit_date", "score", "rank",
                         "fold", "stage", "fit_scope", "selection_usage"} | set(MARKET_FEATURE_COLUMNS)
            if any(name in protected or name.startswith((
                    "raw_", "EXCESS_RET", "STOCK_FWD_RET", "BENCHMARK_FWD_RET")) for name in names):
                raise ConfigurationError("Factor ID collides with protected dataset fields")
            self.factor_names = names
            if options.entry_price != "next_open":
                raise ConfigurationError("Close-known factors require next_open entry")
        if not self.factor_names:
            raise ConfigurationError("At least one factor is required")

    def _binding(self, dates):
        # Canonical JSON types also make persisted tuples compare with lists.
        return json.loads(json.dumps(binding(
            self.snapshot, self.factor_refs, self.data_context, self.data_version,
            dates, self.calendar_id, replace(self.options, factor_names=self.factor_names),
        )))

    def _v2_calendar(self, calendar):
        values = list(calendar)
        if pd.api.types.is_numeric_dtype(pd.Index(values).dtype):
            raise SchemaError("Numeric dates are not daily session keys")
        dates = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
        if (dates.empty or dates.hasnans or dates.tz is not None
                or not dates.equals(dates.normalize()) or dates.has_duplicates
                or not dates.is_monotonic_increasing):
            raise SchemaError("V2 requires a nonempty, sorted, unique daily calendar")
        return dates

    def build(
        self,
        bars: pd.DataFrame,
        benchmark: pd.DataFrame,
        membership: pd.DataFrame,
        status: pd.DataFrame,
        listings: pd.DataFrame,
        calendar: Iterable[object],
    ) -> pd.DataFrame:
        dates = self._v2_calendar(calendar) if self.snapshot is not None else _calendar(calendar)
        if self.snapshot is not None:
            for frame in (bars, benchmark, membership, status, listings):
                if (not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique
                        or any(type(name) is not str for name in frame.columns)):
                    raise SchemaError("V2 inputs require DataFrames with unique columns")
            if (set(self.factor_names) | {"raw_" + name for name in self.factor_names}).intersection(bars.columns):
                raise ConfigurationError("Factor ID collides with input/protected fields")
            if any(name in {"entry_date", "exit_date"} | (set(MARKET_FEATURE_COLUMNS) - {"date"})
                   or name.startswith(("EXCESS_RET", "STOCK_FWD_RET", "BENCHMARK_FWD_RET"))
                   for name in bars.columns):
                raise SchemaError("Bars contain protected label/market output fields")
            # Validate original keys before reindexing can discard invalid rows.
            if not {"date", "symbol"}.issubset(bars.columns):
                raise SchemaError("Bars require date and symbol")
            original_dates = pd.to_datetime(bars["date"], errors="raise")
            if (pd.api.types.is_numeric_dtype(bars["date"].dtype)
                    or original_dates.dt.tz is not None or original_dates.isna().any()
                    or not original_dates.equals(original_dates.dt.normalize())
                    or not original_dates.isin(dates).all()
                    or bars["symbol"].map(lambda v: type(v) is not str or not v.strip()).any()):
                raise SchemaError("Invalid V2 daily security-date keys/calendar")
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
        if self.snapshot is not None and (len(bars) > 250000 or len(dates) * len(symbols) > 250000):
            raise SchemaError("V2 dense panel limit is 250000 rows")
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
        if self.snapshot is None:
            factor_frame = compute_factors(full_bars, self.factor_names)
        else:
            factors = self.snapshot.compute(
                full_bars, self.factor_refs, calendar=dates, context=self.data_context,
            )
            factor_frame = full_bars.merge(factors, on=["date", "symbol"],
                                          how="left", validate="one_to_one")
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
            self.factor_names,
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
        result = selected.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
        if self.snapshot is not None:
            result.attrs[IDENTITY_ATTR] = seal({
                **self._binding(dates),
                "research_interval": {
                    "start": research_dates.min().date().isoformat(),
                    "end": research_dates.max().date().isoformat(),
                },
                "input_hashes": {name: frame_hash(frame) for name, frame in (
                    ("bars", bars), ("benchmark", benchmark), ("membership", membership),
                    ("status", status), ("listings", listings),
                )},
                "dataset_content_hash": frame_hash(result),
            })
        return result

    def write_artifacts(
        self,
        dataset: pd.DataFrame,
        processed_path: Path,
        features_dir: Path,
    ) -> Path | None:
        """Atomically persist raw factors, processed factors, and full dataset."""

        destination = Path(processed_path)
        feature_destination = Path(features_dir)
        identity = None
        if self.snapshot is not None:
            identity = check_identity(dataset.attrs.get(IDENTITY_ATTR))
            # The build receipt must belong to this builder, even before writing.
            expected = self._binding(None)
            if any(identity.get(k) != v for k, v in expected.items() if k != "calendar"):
                raise SchemaError("Dataset identity does not match builder")
            if frame_hash(dataset) != identity["dataset_content_hash"]:
                raise SchemaError("Dataset changed after V2 build")
            targets = [destination, destination.with_suffix(".identity.json"),
                       feature_destination / "raw_factors.parquet",
                       feature_destination / "processed_factors.parquet"]
            if len({path.resolve() for path in targets}) != len(targets):
                raise SchemaError("Dataset artifact destinations must be distinct")
        destination.parent.mkdir(parents=True, exist_ok=True)
        feature_destination.mkdir(parents=True, exist_ok=True)
        raw_names = ["raw_" + name for name in self.factor_names]
        keys = ["date", "symbol"]
        self._atomic_write(dataset[keys + raw_names], feature_destination / "raw_factors.parquet")
        self._atomic_write(
            dataset[keys + list(self.factor_names)],
            feature_destination / "processed_factors.parquet",
        )
        self._atomic_write(dataset, destination)
        if identity is not None:
            identity.pop("artifacts", None)
            identity["artifacts"] = {
                "dataset": file_hash(destination),
                "raw_features": file_hash(feature_destination / "raw_factors.parquet"),
                "processed_features": file_hash(feature_destination / "processed_factors.parquet"),
            }
            sidecar = destination.with_suffix(".identity.json")
            temporary = sidecar.with_name(sidecar.name + ".tmp")
            temporary.write_text(json.dumps(seal(identity), indent=2, sort_keys=True,
                                            allow_nan=False) + "\n", encoding="utf-8")
            os.replace(temporary, sidecar)
            return sidecar
        return None

    def read_artifacts(self, processed_path, features_dir, *, calendar,
                       expected_identity_hash=None, expected_input_hashes=None):
        """Require the V2 sidecar and verify artifacts before model consumption."""
        if self.snapshot is None:
            raise ConfigurationError("Verified artifact loading requires V2 configuration")
        return verify_dataset_artifacts(
            processed_path, features_dir,
            expected_binding=self._binding(self._v2_calendar(calendar)),
            expected_identity_hash=expected_identity_hash,
            expected_input_hashes=expected_input_hashes,
        )

    @staticmethod
    def _atomic_write(frame: pd.DataFrame, destination: Path) -> None:
        temporary = destination.with_name(destination.name + ".tmp")
        frame.to_parquet(temporary, index=False)
        os.replace(str(temporary), str(destination))
