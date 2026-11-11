"""Run with: python -m factorlab.trading.demo"""
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import Market, OrderIntent, OrderStatus, OrderType, SAFETY_LABELS, Side
from .control import RiskLimits, TradingController
from .paper import Instrument, PaperBroker, Quote
from .storage import PaperStore


def run_demo(market: Market, path: Path) -> dict[str, str]:
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    currency = "USDT" if market == Market.BINANCE_USDM else "USD"
    limits = RiskLimits(D("300"), D("500"), D("800"), 10)

    def broker():
        return PaperBroker(market, "synthetic-demo", (Instrument("SYNTHETIC", currency),), {currency: D("1000")})

    store = PaperStore(path)
    control = TradingController(broker(), store, limits=limits, clock=lambda: now)
    control.quote(Quote("SYNTHETIC", D("100"), now))
    strategy_intent = OrderIntent(market, "SYNTHETIC", Side.BUY, OrderType.LIMIT,
                                 D("2"), D("100"), "demo-open")
    decision = control.assess(strategy_intent)
    assert decision.approved
    control.submit(strategy_intent)
    partial = control.fill("demo-open", D("0.75"))
    assert partial.status == OrderStatus.PARTIALLY_FILLED
    assert control.replay(partial) is False
    control.fill("demo-open", D("1.25"))
    account = control.reconcile()
    assert account.positions[0].quantity == D("2")
    assert account.positions[0].entry_price == D("100")
    expected = D("799.8") if market == Market.BINANCE_EQUITY else D("999.8")
    assert account.assets[0].total == expected
    assert account.assets[0].reserved == (D("0") if market == Market.BINANCE_EQUITY else D("200"))
    store.close()

    # Restart gates new orders until the paper authority is reconciled.
    store = PaperStore(path)
    control = TradingController(broker(), store, limits=limits, clock=lambda: now)
    assert not control.account().reconciled
    assert control.reconcile().reconciled
    control.quote(Quote("SYNTHETIC", D("110"), now))
    control.set_kill_switch()
    closing = OrderIntent(market, "SYNTHETIC", Side.SELL, OrderType.MARKET,
                          D("2"), None, "demo-close", reduce_only=True)
    control.submit(closing)
    control.fill("demo-close", D("2"))
    account = control.reconcile()
    fees = sum((r.snapshot.cumulative_fee for r in control.broker.state.orders.values()), D("0"))
    assert fees == D("0.42")
    assert account.assets[0].total == D("1019.58")
    assert account.assets[0].reserved == 0
    assert not account.positions and not account.open_orders
    assert all(r.snapshot.status == OrderStatus.FILLED for r in control.broker.state.orders.values())
    result = {"market": market.value, "currency": currency, "cash": str(account.assets[0].total),
              "fees": str(fees), "positions": "0", "orders": "FILLED", "reconciled": str(account.reconciled)}
    store.close()
    return result


def main():
    print(" / ".join(SAFETY_LABELS))
    with TemporaryDirectory(prefix="factorlab-paper-") as root:
        for market in Market:
            print(run_demo(market, Path(root) / f"{market.value}.sqlite"))


if __name__ == "__main__":
    main()
