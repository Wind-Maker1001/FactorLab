"""Run with python -m factorlab.integrations.binance_equity_trading.demo."""
from datetime import datetime, timezone
from decimal import Decimal as D

from factorlab.integrations.binance_equity.market import stream_rows
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.storage import PaperStore

from . import SAFETY_LABELS
from .paper import Observations, StockAdapter, StockPaperBroker
from .protocol import Rules


def run():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    ms = int(now.timestamp() * 1000)
    # Synthetic inputs in the existing Equity wire format, not captured live prices.
    _, quotes = stream_rows(dict(e="quote", s="NVDA", E=ms, T=ms,
                                  bp="149.99", ap="150.00", bs="10", **{"as": "10"}),
                            now.isoformat(), "synthetic:NVDA")
    _, sessions = stream_rows(dict(e="calendar", ts=ms, **{"from": "PRE", "to": "O"}),
                              now.isoformat(), "synthetic:calendar")
    rules = Rules.from_exchange_info(dict(symbol="NVDA", tradability="BUY_SELL",
        stepSize="0.001", minQty="0.001", maxQty="1000", minNotional="1", maxNotional="100000",
        fractionable=True, fractionableEh=True, extendedSession=True, overnightSupported=True,
        maxNumOrders=100, multiplierDown="0.5", multiplierUp="1.5"))
    broker = StockPaperBroker("synthetic-usd", (Instrument("NVDA", "USD"),), {"USD": D("1000")}, fee_rate=D("0.001"))
    store = PaperStore(":memory:")
    try:
        controller = TradingController(broker, store, clock=lambda: now,
            limits=RiskLimits(D("200"), D("500"), D("1000"), 10))
        adapter = StockAdapter(controller)
        obs = Observations.from_equity(quotes[0], sessions[0], rules,
            rules_time=ms, rules_received_at=now, permission=True, reference_price=D("150"))
        # Illustrative signal: buy half a share below a configured ask threshold.
        if obs.price > D("151"):
            raise ValueError("no strategy signal")
        intent = OrderIntent(Market.BINANCE_EQUITY, "NVDA", Side.BUY, OrderType.MARKET,
                             D("0.5"), None, "nvda-demo-000000000000000000000001")
        controller.quote(Quote("NVDA", obs.price, now))
        params = broker.bind(intent, obs, notional=D("75"))
        decision = controller.assess(intent)
        event = adapter.submit_order(intent)
        controller.fill(intent.client_order_id, D("0.2"))
        fill = controller.fill(intent.client_order_id, D("0.3"))
        account = adapter.reconcile(adapter.read_account())
        return dict(labels=SAFETY_LABELS, mode="SYNTHETIC_USD_NOT_EXCHANGE_ELIGIBILITY",
                    signal="BUY 0.5 NVDA", risk=decision.reason, request=params,
                    submitted=event.status.value, fill=fill.status.value,
                    cash=str(account.assets[0].total), reserved=str(account.assets[0].reserved),
                    shares=str(account.positions[0].quantity), fee=str(fill.cumulative_fee),
                    reconciled=account.reconciled, settlement=None)
    finally:
        store.db.close()


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
