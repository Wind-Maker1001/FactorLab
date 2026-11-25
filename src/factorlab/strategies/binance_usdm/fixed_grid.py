"""Fixed arithmetic Grid control group (R08/R09/R10 incumbent) for Binance USD-M NVDAUSDT."""


class FixedGridCandidate:
    strategy_family = "fixed_grid"
    s03_compatible = True
    s03_entrypoint = "LEGACY_DEMO_GRID_BRIDGE"

    def __init__(
        self,
        symbol: str = "NVDAUSDT",
        interval_seconds: int = 3600,
        half_width_pct: float = 0.01,
        order_ttl: int = 100,
        breakout: str = "pause",
    ):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("FixedGridCandidate supports NVDAUSDT with positive interval_seconds only")
        if not (0.002 <= float(half_width_pct) <= 0.20):
            raise ValueError("half_width_pct must be within [0.002, 0.20]")
        if type(order_ttl) is not int or order_ttl < 1:
            raise ValueError("order_ttl must be a positive integer")
        if breakout not in {"pause", "exit"}:
            raise ValueError("breakout must be pause or exit")
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.half_width_pct = float(half_width_pct)
        self.order_ttl = order_ttl
        self.breakout = breakout

    def normalized_parameters(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "half_width_pct": self.half_width_pct,
            "order_ttl": self.order_ttl,
            "breakout": self.breakout,
        }

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="fixed_grid",
            version="1.0.0",
            decision_kind="orders",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar", "quote", "order_event"],
            history_limit=1,
        )
