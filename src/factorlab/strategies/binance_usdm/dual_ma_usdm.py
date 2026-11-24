"""S01 Dual Moving Average target strategy for Binance USD-M NVDAUSDT 1x long-only.

Implements both S01 UnifiedStrategy (describe/decide) and self-contained S03
factorlab.demo.evaluate/v1 worker entrypoint so winning packages can be verified
and installed without external dependencies.
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
    s03_entrypoint = "dual_ma_usdm:evaluate"

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


def evaluate(context: dict, state: dict) -> dict:
    """S03 factorlab.demo.evaluate/v1 entrypoint for Demo closed-bar execution."""
    if context.get("event") == "probe":
        return {"state": dict(state), "intents": []}
    cfg = context.get("configuration", {})
    fast_window = int(cfg.get("fast_window", 5))
    slow_window = int(cfg.get("slow_window", 20))
    order_qty = Decimal(str(cfg.get("order_quantity", "0.04")))
    bar = context.get("bar", {})
    close = float(bar["close"])
    closes = list(state.get("closes", []))
    closes.append(close)
    closes = closes[-(slow_window + 1):]
    sig = _crossover_signal(closes, fast_window, slow_window)
    account = context.get("account", {})
    positions = account.get("positions", [])
    held = sum((Decimal(str(p.get("quantity", "0"))) for p in positions), Decimal("0"))
    intents = []
    if sig == "BUY" and held <= Decimal("0"):
        intents.append({"side": "BUY", "quantity": str(order_qty)})
    elif sig == "SELL" and held > Decimal("0"):
        intents.append({"side": "SELL", "quantity": str(held)})
    return {"state": {"closes": closes, "decisions": int(state.get("decisions", 0)) + 1}, "intents": intents}
