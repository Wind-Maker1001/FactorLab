"""v49 adds account, quote, and weekly-decision guards to the v48 strategy."""
import numpy as np
import pandas as pd
import qlib
from qlib.backtest import backtest
from qlib.backtest.decision import Order, TradeDecisionWO

from v48_strategy import ROOT, TARGET, TICKERS, V48Strategy

CAPITAL = 300_000


class V49Strategy(V48Strategy):
    def generate_trade_decision(self, execute_result=None):
        start, end = self.trade_calendar.get_step_time()
        day = start.normalize()
        iso = day.isocalendar()
        week = (iso.year, iso.week)
        if week == self.last_week:
            return TradeDecisionWO([], self)

        day_index = self.close.index.get_loc(day)
        if day_index == 0:
            raise ValueError("回测起点前至少需要一个交易日的行情")
        previous = self.close.index[day_index - 1]
        prices = self.close.loc[previous, TICKERS]
        volumes = self.volume.loc[previous, TICKERS]

        position = self.trade_position
        all_holdings = position.get_stock_amount_dict()
        unexpected = sorted(set(all_holdings) - set(TICKERS))
        if unexpected:
            raise ValueError(f"存在目标股票池外持仓：{', '.join(unexpected)}")

        held = pd.Series(all_holdings, dtype=float).reindex(TICKERS, fill_value=0.0)
        valid_prices = np.isfinite(prices) & prices.gt(0.0)
        valid_quotes = valid_prices & np.isfinite(volumes) & volumes.ge(0.0)
        invalid_held = held.ne(0.0) & ~valid_quotes
        if invalid_held.any():
            tickers = ", ".join(invalid_held.index[invalid_held])
            raise ValueError(f"{tickers} 持仓缺少有效行情")

        valid_trend = prices.gt(self.ma28.loc[previous])
        weights = TARGET.where(valid_trend, 0.0)
        if not (valid_prices["QQQ"] and prices["QQQ"] > self.qqq_ma63.loc[previous]):
            weights.loc[["TQQQ", "QQQ"]] = 0.0

        held_value = (held * prices.where(valid_quotes)).fillna(0.0)
        nav = float(position.get_cash() + held_value.sum())
        sells, buys = [], []

        for ticker in TICKERS:
            if not valid_quotes[ticker]:
                continue

            weight_change = float(weights[ticker] - held_value[ticker] / nav)
            if weights[ticker] == 0:
                amount = -held[ticker]
            elif abs(weight_change) >= 0.005:
                amount = weight_change * nav / prices[ticker]
            else:
                continue

            quantity = min(abs(amount), 0.001 * volumes[ticker])
            if quantity <= 1e-8:
                continue

            if amount < 0:
                sells.append(Order(ticker, quantity, Order.SELL, start_time=start, end_time=end))
            else:
                buys.append(Order(ticker, quantity, Order.BUY, start_time=start, end_time=end))

        decision = TradeDecisionWO(sells + buys, self)
        self.last_week = week
        return decision


def run_v49(start="2025-01-01", end="2026-11-15", *, cost=0.001, provider_name="provider"):
    qlib.init(
        provider_uri=str(ROOT / provider_name), region="us", kernels=1,
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

    strategy = V49Strategy(pd.DataFrame(closes), pd.DataFrame(volumes))
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
        account=CAPITAL,
        benchmark="QQQ",
        exchange_kwargs={
            "freq": "day", "codes": TICKERS, "deal_price": "open",
            "limit_threshold": None, "trade_unit": None, "min_cost": 0,
            "open_cost": cost, "close_cost": cost,
        },
    )
    report, _ = reports["1day"]
    return report, pd.DataFrame(strategy.fills)


if __name__ == "__main__":
    daily, fills = run_v49()
    pnl = daily["account"].iloc[-1] - CAPITAL
    print(f"v49 | {len(daily)} sessions | {len(fills)} fills | {pnl:+,.2f} USD | {pnl / CAPITAL:+.2%}")
