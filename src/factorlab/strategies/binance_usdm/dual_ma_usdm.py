"""S01 Dual Moving Average target strategy for Binance USD-M NVDAUSDT 1x long-only.

Implements the native S01/S04 FACTORLAB_DECISION_V1 protocol (describe/decide)
with target_weight execution so research and Demo release use the exact same
decision class, parameters, interval constraints, and target-position sizing.
"""
from decimal import Decimal


def _sma(values: list[float], window: int) -> float:
    return sum(values[-window:]) / float(window)


def _crossover_signal(closes: list[float], fast_window: int, slow_window: int) -> str:
    if len(closes) < slow_window + 1:
        return "HOLD"
    prev_fast = _sma(closes[:-1], fast_window)
    prev_slow = _sma(closes[:-1], slow_window)
    curr_fast = _sma(closes, fast_window)
    curr_slow = _sma(closes, slow_window)
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return "BUY"
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return "SELL"
    return "HOLD"


class DualMAUsdmStrategy:
    strategy_family = "target_protocol"
    s03_compatible = True
    s03_protocol = "FACTORLAB_DECISION_V1"
    s03_entrypoint = "dual_ma_usdm:DualMAUsdmStrategy"

    def __init__(
        self,
        symbol: str = "NVDAUSDT",
        interval_seconds: int = 3600,
        fast_window: int = 5,
        slow_window: int = 20,
        max_weight: float = 0.90,
    ):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("DualMAUsdmStrategy supports NVDAUSDT with positive interval_seconds only")
        if type(fast_window) is not int or type(slow_window) is not int or not (2 <= fast_window < slow_window <= 500):
            raise ValueError("require integer 2 <= fast_window < slow_window <= 500")
        if not (0.0 < float(max_weight) <= 1.0):
            raise ValueError("max_weight must be in (0, 1]")
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.max_weight = float(max_weight)

    def normalized_parameters(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "max_weight": self.max_weight,
        }

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="dual_ma",
            version="1.0.0",
            decision_kind="target",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar"],
            history_limit=self.slow_window + 1,
        )

    def decide(self, event: dict, state: dict):
        from factorlab.strategies.protocol import Decision
        bars = event.get("bars", [])
        closes = [float(b["close"]) for b in bars]
        sig = _crossover_signal(closes, self.fast_window, self.slow_window)
        next_state = {"decisions": int(state.get("decisions", 0)) + 1, "last_signal": sig}
        if sig == "BUY":
            return Decision(target_weight=Decimal(str(self.max_weight)), state=next_state)
        if sig == "SELL":
            return Decision(target_weight=Decimal("0"), state=next_state)
        return Decision(target_weight=None, state=next_state)
