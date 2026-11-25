"""Cash / zero-position benchmark for Binance USD-M NVDAUSDT 1x long-only research.

Implements the native S01/S04 FACTORLAB_DECISION_V1 protocol (describe/decide)
returning target_weight=0 so research and Demo release stay strictly equivalent.
"""
from decimal import Decimal


class CashBenchmarkStrategy:
    strategy_family = "cash"
    s03_compatible = True
    s03_protocol = "FACTORLAB_DECISION_V1"
    s03_entrypoint = "cash_benchmark:CashBenchmarkStrategy"

    def __init__(self, symbol: str = "NVDAUSDT", interval_seconds: int = 3600):
        if symbol != "NVDAUSDT" or type(interval_seconds) is not int or interval_seconds <= 0:
            raise ValueError("CashBenchmarkStrategy supports NVDAUSDT with positive interval_seconds only")
        self.symbol = symbol
        self.interval_seconds = interval_seconds

    def normalized_parameters(self) -> dict:
        return {"symbol": self.symbol, "interval_seconds": self.interval_seconds}

    def describe(self):
        from factorlab.strategies.protocol import StrategySpec
        return StrategySpec(
            strategy_id="cash",
            version="1.0.0",
            decision_kind="target",
            symbols=[self.symbol],
            intervals=[self.interval_seconds],
            data_types=["closed_bar"],
            history_limit=1,
        )

    def decide(self, event: dict, state: dict):
        from factorlab.strategies.protocol import Decision
        next_state = {"bars": int(state.get("bars", 0)) + 1}
        return Decision(target_weight=Decimal("0"), state=next_state)
