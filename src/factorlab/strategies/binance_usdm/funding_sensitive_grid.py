"""R10 Causal Funding-Sensitive Grid candidate for Binance USD-M NVDAUSDT."""


class FundingSensitiveGridCandidate:
    strategy_family = "funding_sensitive_grid"
    s03_compatible = False
    s03_entrypoint = None
    s04_requirements = (
        "Requires causal settled funding event feed in Demo runtime context and "
        "GridExecution multi-order bridge wiring (owned by S04)."
    )

    def __init__(
        self,
        symbol: str = "NVDAUSDT",
        interval_seconds: int = 3600,
        profile_id: str = "B5",
        half_width_pct: float = 0.01,
        lookback_hours: int = 24,
        conservative: bool = True,
        entry_max_bps_h: float = 0.50,
        expected_hold_hours: float = 48.0,
        min_net_margin_bps: float = 100.0,
        exit_mode: str = "funding_bleed",
        exit_bps_h: float = 1.00,
        max_hold_hours: float = 72.0,
    ):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("FundingSensitiveGridCandidate supports NVDAUSDT with positive interval_seconds only")
        if exit_mode not in {"none", "funding_bleed", "net_margin"}:
            raise ValueError("unsupported exit_mode")
        if type(lookback_hours) is not int or lookback_hours < 1:
            raise ValueError("lookback_hours must be >= 1")
        if not (0.002 <= float(half_width_pct) <= 0.20):
            raise ValueError("half_width_pct must be within [0.002, 0.20]")
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.profile_id = str(profile_id)
        self.half_width_pct = float(half_width_pct)
        self.lookback_hours = lookback_hours
        self.conservative = bool(conservative)
        self.entry_max_bps_h = float(entry_max_bps_h)
        self.expected_hold_hours = float(expected_hold_hours)
        self.min_net_margin_bps = float(min_net_margin_bps)
        self.exit_mode = exit_mode
        self.exit_bps_h = float(exit_bps_h) if exit_bps_h >= 0 else -1.0
        self.max_hold_hours = float(max_hold_hours) if max_hold_hours > 0 else 0.0

    def normalized_parameters(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "profile_id": self.profile_id,
            "half_width_pct": self.half_width_pct,
            "lookback_hours": self.lookback_hours,
            "conservative": self.conservative,
            "entry_max_bps_h": self.entry_max_bps_h,
            "expected_hold_hours": self.expected_hold_hours,
            "min_net_margin_bps": self.min_net_margin_bps,
            "exit_mode": self.exit_mode,
            "exit_bps_h": self.exit_bps_h,
            "max_hold_hours": self.max_hold_hours,
        }

    def to_funding_policy(self):
        from factorlab.experiments.grid_funding_adaptive import FundingPolicy
        return FundingPolicy(
            candidate_id=self.profile_id,
            lookback_hours=self.lookback_hours,
            conservative=self.conservative,
            entry_max_bps_h=self.entry_max_bps_h,
            expected_hold_hours=self.expected_hold_hours,
            min_net_margin_bps=self.min_net_margin_bps,
            exit_mode=self.exit_mode,
            exit_bps_h=None if self.exit_mode == "none" or self.exit_bps_h < 0 else self.exit_bps_h,
            max_hold_hours=None if self.exit_mode == "none" or self.max_hold_hours <= 0 else self.max_hold_hours,
        )

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="funding_sensitive_grid",
            version="1.0.0",
            decision_kind="orders",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar", "quote", "order_event"],
            history_limit=1,
        )
