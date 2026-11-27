"""Qlib v48: weekly US equity trends with a separate Nasdaq ETF filter."""
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest
from qlib.backtest.decision import Order, TradeDecisionWO
from qlib.strategy.base import BaseStrategy

TICKERS = ["AAPL", "SNDK", "BA", "C", "JPM", "GE", "TQQQ", "QQQ"]
TARGET = pd.Series(0.04, index=TICKERS)
TARGET.loc["TQQQ"] = 0.04 / 3
ROOT = Path(__file__).resolve().parent


class V48Strategy(BaseStrategy):
    def __init__(self, adjusted_close, adjusted_volume):
        super().__init__()
        self.close = adjusted_close
        self.volume = adjusted_volume
        self.ma28 = self.close.rolling(28, min_periods=28).mean()
        self.qqq_ma63 = self.close["QQQ"].rolling(63, min_periods=63).mean()
        self.last_week = None
        self.fills = []

    def generate_trade_decision(self, execute_result=None):
        start, end = self.trade_calendar.get_step_time()
        day = start.normalize()
        iso = day.isocalendar()
        week = (iso.year, iso.week)
        if week == self.last_week:
            return TradeDecisionWO([], self)
        self.last_week = week

        day_index = self.close.index.get_loc(day)
        if day_index == 0:
            raise ValueError("回测起点前至少需要一个交易日的行情")
        previous = self.close.index[day_index - 1]
        close = self.close.loc[previous]
        valid_trend = close.gt(self.ma28.loc[previous])
        weights = TARGET.where(valid_trend, 0.0)

        if not (close["QQQ"] > self.qqq_ma63.loc[previous]):
            weights.loc[["TQQQ", "QQQ"]] = 0.0

        position = self.trade_position
        held = pd.Series(position.get_stock_amount_dict(), dtype=float)
        held = held.reindex(TICKERS, fill_value=0.0)
        held_value = (held * close).fillna(0.0)
        nav = float(position.get_cash() + held_value.sum())
        sells, buys = [], []

        for ticker in TICKERS:
            price = close[ticker]
            volume = self.volume.at[previous, ticker]
            if not (np.isfinite(price) and np.isfinite(volume)):
                if held[ticker] != 0:
                    raise ValueError(f"{ticker} 持仓缺少上一交易日有效行情")
                continue

            weight_change = float(weights[ticker] - held_value[ticker] / nav)
            if weights[ticker] == 0:
                amount = -held[ticker]
            elif abs(weight_change) >= 0.005:
                amount = weight_change * nav / price
            else:
                continue

            quantity = min(abs(amount), 0.001 * volume)
            if quantity <= 1e-8:
                continue

            if amount < 0:
                sells.append(Order(ticker, quantity, Order.SELL, start_time=start, end_time=end))
            else:
                buys.append(Order(ticker, quantity, Order.BUY, start_time=start, end_time=end))

        return TradeDecisionWO(sells + buys, self)

    def post_exe_step(self, execute_result):
        for order, turnover, fee, price in execute_result:
            self.fills.append({
                "date": order.start_time.normalize(),
                "ticker": order.stock_id,
                "side": "buy" if order.direction == Order.BUY else "sell",
                "quantity": order.deal_amount,
                "requested": order.amount,
                "price": price,
                "turnover": turnover,
                "fee": fee,
            })


def run_v48(start="2025-01-01", end="2026-11-15"):
    qlib.init(
        provider_uri=str(ROOT / "provider"), region="us", kernels=1,
        expression_cache=None, dataset_cache=None, logging_level=40,
    )
    closes, volumes = {}, {}
    for ticker in TICKERS:
        history = pd.read_parquet(
            ROOT / "data" / f"{ticker}.parquet",
            columns=["adjusted", "volume", "adjust_ratio"],
        )
        closes[ticker] = history["adjusted"]
        volumes[ticker] = history["volume"] / history["adjust_ratio"]

    strategy = V48Strategy(pd.DataFrame(closes), pd.DataFrame(volumes))
    reports, _ = backtest(
        start_time=start,
        end_time=end,
        strategy=strategy,
        executor={
            "class": "SimulatorExecutor",
            "module_path": "qlib.backtest.executor",
            "kwargs": {
                "time_per_step": "day",
                "generate_portfolio_metrics": True,
                "trade_type": "serial",
            },
        },
        account=300_000,
        benchmark="QQQ",
        exchange_kwargs={
            "freq": "day", "codes": TICKERS, "deal_price": "open",
            "limit_threshold": None, "trade_unit": None, "min_cost": 0,
            "open_cost": 0.001, "close_cost": 0.001,
        },
    )
    report, _ = reports["1day"]
    return report, pd.DataFrame(strategy.fills)


if __name__ == "__main__":
    daily, fills = run_v48()
    pnl = daily["account"].iloc[-1] - 300_000
    print(f"v48 | {len(daily)} sessions | {pnl:+,.2f} USD | {pnl / 300_000:+.2%}")
