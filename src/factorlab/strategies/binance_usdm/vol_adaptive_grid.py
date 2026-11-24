"""R10 Causal Volatility-Adaptive Grid candidate for Binance USD-M NVDAUSDT.

Reference volatility sigma_ref is fitted strictly on the training window only
and frozen before validation and sealed test windows.
"""


class VolAdaptiveGridCandidate:
    strategy_family = "vol_adaptive_grid"
    s03_compatible = False
    s03_entrypoint = None
    s04_requirements = (
        "Requires causal rolling volatility state + frozen training sigma_ref in "
        "Demo runtime context and GridExecution bridge wiring (owned by S04)."
    )

    def __init__(
        self,
        symbol: str = "NVDAUSDT",
        interval_seconds: int = 3600,
        profile_id: str = "C5",
        vol_window_hours: int = 48,
        k_mult: float = 3.0,
        horizon_hours: float = 1.0,
        min_half_width: float = 0.006,
        max_half_width: float = 0.035,
        extreme_vol_mult: float = 2.0,
        allow_flat_rebuild: bool = True,
        min_rebuild_interval: int = 24,
        rebuild_width_delta: float = 0.25,
    ):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("VolAdaptiveGridCandidate supports NVDAUSDT with positive interval_seconds only")
        if type(vol_window_hours) is not int or vol_window_hours < 2:
            raise ValueError("vol_window_hours must be >= 2")
        if not (0.002 <= float(min_half_width) <= float(max_half_width) <= 0.20):
            raise ValueError("invalid half-width bounds")
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.profile_id = str(profile_id)
        self.vol_window_hours = vol_window_hours
        self.k_mult = float(k_mult)
        self.horizon_hours = float(horizon_hours)
        self.min_half_width = float(min_half_width)
        self.max_half_width = float(max_half_width)
        self.extreme_vol_mult = float(extreme_vol_mult)
        self.allow_flat_rebuild = bool(allow_flat_rebuild)
        self.min_rebuild_interval = int(min_rebuild_interval)
        self.rebuild_width_delta = float(rebuild_width_delta)

    def normalized_parameters(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "profile_id": self.profile_id,
            "vol_window_hours": self.vol_window_hours,
            "k_mult": self.k_mult,
            "horizon_hours": self.horizon_hours,
            "min_half_width": self.min_half_width,
            "max_half_width": self.max_half_width,
            "extreme_vol_mult": self.extreme_vol_mult,
            "allow_flat_rebuild": self.allow_flat_rebuild,
            "min_rebuild_interval": self.min_rebuild_interval,
            "rebuild_width_delta": self.rebuild_width_delta,
        }

    def to_vol_policy(self, *, reference_vol_1h: float):
        from factorlab.experiments.grid_funding_adaptive import VolatilityPolicy
        return VolatilityPolicy(
            candidate_id=self.profile_id,
            vol_window_hours=self.vol_window_hours,
            k_mult=self.k_mult,
            horizon_hours=self.horizon_hours,
            min_half_width=self.min_half_width,
            max_half_width=self.max_half_width,
            extreme_vol_mult=None if self.extreme_vol_mult <= 0 else self.extreme_vol_mult,
            allow_flat_rebuild=self.allow_flat_rebuild,
            min_rebuild_interval=self.min_rebuild_interval,
            rebuild_width_delta=self.rebuild_width_delta,
            reference_vol_1h=float(reference_vol_1h),
        )

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="vol_adaptive_grid",
            version="1.0.0",
            decision_kind="orders",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar", "quote", "order_event"],
            history_limit=self.vol_window_hours + 1,
        )
