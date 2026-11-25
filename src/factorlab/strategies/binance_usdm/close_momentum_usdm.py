"""S01 CloseMomentum custom trend target strategy for Binance USD-M NVDAUSDT 1x long-only.

Implements the native S01/S04 FACTORLAB_DECISION_V1 protocol (describe/decide)
with target_weight execution matching examples/protocol_strategy/close_momentum_v1
with configurable lookback, weight, and threshold_bps.
"""
from decimal import Decimal


class CloseMomentumUsdmStrategy:
    strategy_family = "target_protocol"
    s03_compatible = True
    s03_protocol = "FACTORLAB_DECISION_V1"
    s03_entrypoint = "close_momentum_usdm:CloseMomentumUsdmStrategy"

    def __init__(
        self,
        symbol: str = "NVDAUSDT",
        interval_seconds: int = 3600,
        lookback: int = 1,
        weight: float = 0.90,
        threshold_bps: float = 0.0,
    ):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("CloseMomentumUsdmStrategy supports NVDAUSDT with positive interval_seconds only")
        if type(lookback) is not int or not (1 <= lookback <= 500):
            raise ValueError("lookback must be an integer within [1, 500]")
        if not (0.0 < float(weight) <= 1.0):
            raise ValueError("weight must be in (0, 1]")
        if float(threshold_bps) < 0.0:
            raise ValueError("threshold_bps must be nonnegative")
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.lookback = lookback
        self.weight = float(weight)
        self.threshold_bps = float(threshold_bps)

    def normalized_parameters(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "lookback": self.lookback,
            "weight": self.weight,
            "threshold_bps": self.threshold_bps,
        }

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="close_momentum",
            version="1.0.0",
            decision_kind="target",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar"],
            history_limit=self.lookback + 1,
        )

    def decide(self, event: dict, state: dict):
        from factorlab.strategies.protocol import Decision
        bars = event.get("bars", [])
        next_state = {"decisions": int(state.get("decisions", 0)) + 1}
        if len(bars) < self.lookback + 1:
            return Decision(state=next_state)
        latest = Decimal(str(bars[-1]["close"]))
        prior = Decimal(str(bars[-(self.lookback + 1)]["close"]))
        ret_bps = (latest - prior) / prior * Decimal("10000")
        target = Decimal(str(self.weight)) if ret_bps > Decimal(str(self.threshold_bps)) else Decimal("0")
        return Decision(target_weight=target, state=next_state)
