"""Native fixed-band V69; provenance in tests/fixtures/v69_original.
No Qlib or XGBoost runtime dependency. Preserve original float arithmetic.
"""
import numpy as np
import pandas as pd
TICKERS = ['AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ']
TARGET = pd.Series(0.04, index=TICKERS)
TARGET.loc['TQQQ'] = 0.04 / 3

class Band:
    def __init__(self, adjusted_close, adjusted_volume, adjusted_open=None, *, omit_sndk=False):
        for frame in (adjusted_close, adjusted_volume):
            if list(frame.columns) != TICKERS or not frame.index.equals(adjusted_close.index):
                raise ValueError('aligned complete original ordered universe required')
            if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                raise ValueError('unique ordered session index required')
        self.close = adjusted_close.copy()
        self.volume = adjusted_volume.copy()
        self.omit_sndk = omit_sndk
        self.ma28 = self.close.rolling(28, min_periods=28).mean()
        self.qqq_ma63 = self.close.QQQ.rolling(63, min_periods=63).mean()
        self.ma63 = self.close.rolling(63).mean()
        self.vol = self.close.pct_change(fill_method=None).rolling(21).std()
        eligible = self.eligible_symbols()
        ready = self.ma28[eligible].notna() & self.close[eligible].gt(0) & np.isfinite(self.close[eligible])
        self.breadth = (self.close[eligible].gt(self.ma28[eligible]) & ready).sum(axis=1) / ready.sum(axis=1).replace(0, np.nan)

    def eligible_symbols(self):
        return [s for s in self.close if s != 'SNDK' or not self.omit_sndk]

    def targets(self, previous, prices, valid_prices, held):
        fast = self.ma28.loc[previous]
        slow = self.ma63.loc[previous]
        above = prices.gt(fast)
        qqq_on = valid_prices["QQQ"] and prices["QQQ"] > self.qqq_ma63.at[previous]
        bandwidth = 0.5 * self.vol.loc[previous]
        enter = prices.gt(fast * (1 + bandwidth))
        stay = held.gt(0) & prices.ge(fast * (1 - bandwidth))
        weights = TARGET.where(enter | stay, 0.0)
        if qqq_on and self.breadth.at[previous] >= 2 / 3:
            weights *= 1.10
        if not qqq_on:
            weights.loc[["QQQ", "TQQQ"]] = 0.0
        if self.omit_sndk:
            weights["SNDK"] = 0.0
        return weights.where(valid_prices, 0.0)

class WeakExit(Band):
    def targets(self, previous, prices, valid_prices, held):
        w = super().targets(previous, prices, valid_prices, held)
        weak = self.breadth.at[previous] < .5 or prices.QQQ <= self.qqq_ma63.at[previous]
        if weak:
            w = w.mask(held.gt(0) & prices.le(self.ma28.loc[previous]), 0.0)
        return w

class Scaled(WeakExit):
    def targets(self, previous, prices, valid_prices, held):
        return 1.10 * super().targets(previous,prices,valid_prices,held)

class NativeV69(Scaled):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        daily=self.close[self.eligible_symbols()].pct_change(fill_method=None)
        market_var=daily.QQQ.rolling(42).var().replace(0,np.nan)
        cov=daily.rolling(42).cov(daily.QQQ)
        residual=(daily.rolling(42).var()-cov.pow(2).div(market_var,axis=0)).clip(lower=0).pow(.5)
        self.high=residual.gt(residual.quantile(.75,axis=1),axis=0).reindex(columns=self.close.columns,fill_value=False)
        cross=self.close.gt(self.ma28*(1+.5*self.vol))
        self.confirmed=cross&cross.shift(1,fill_value=False)

    def targets(self,previous,prices,valid_prices,held):
        w=super().targets(previous,prices,valid_prices,held)
        return w.mask(held.le(0)&self.high.loc[previous]&~self.confirmed.loc[previous],0.)


# Research-unit order records, not broker requests.
from dataclasses import dataclass

@dataclass
class Order:
    SELL = 0
    BUY = 1
    stock_id: str
    amount: float
    direction: int
    start_time: object
    end_time: object

@dataclass
class TradeDecisionWO:
    order_list: list
    strategy: object

class V69Planner(NativeV69):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_week = None
        self.decisions = []

    def generate_trade_decision(self):
        start, end = self.trade_calendar.get_step_time()
        day = start.normalize()
        iso = day.isocalendar()
        week = iso.year, iso.week
        if week == self.last_week:
            return TradeDecisionWO([], self)
        i = self.close.index.get_loc(day)
        if i == 0:
            raise ValueError("回测起点前至少需要一个交易日的行情")
        previous = self.close.index[i - 1]
        prices = self.close.loc[previous, TICKERS]
        volumes = self.volume.loc[previous, TICKERS]
        position = self.trade_position
        holdings = position.get_stock_amount_dict()
        outside = sorted(set(holdings) - set(TICKERS))
        if outside:
            raise ValueError(f"存在目标股票池外持仓：{', '.join(outside)}")
        held = pd.Series(holdings, dtype=float).reindex(TICKERS, fill_value=0.0)
        valid = np.isfinite(prices) & prices.gt(0)
        tradable = valid & np.isfinite(volumes) & volumes.ge(0)
        invalid_held = held.ne(0) & ~tradable
        if invalid_held.any():
            raise ValueError(f"{', '.join(held.index[invalid_held])} 持仓缺少有效行情")
        weights = self.targets(previous, prices, valid, held)
        held_value = (held * prices.where(tradable)).fillna(0.0)
        nav = float(position.get_cash() + held_value.sum())
        sells, buys = [], []
        for symbol in TICKERS:
            if not tradable[symbol]:
                continue
            change = weights[symbol] - held_value[symbol] / nav
            if weights[symbol] == 0:
                amount = -held[symbol]
            elif abs(change) >= 0.005:
                amount = change * nav / prices[symbol]
            else:
                continue
            size = min(abs(amount), 0.001 * volumes[symbol])
            if size <= 1e-8:
                continue
            side = Order.SELL if amount < 0 else Order.BUY
            order = Order(symbol, size, side, start_time=start, end_time=end)
            (sells if amount < 0 else buys).append(order)
        decision = TradeDecisionWO(sells + buys, self)
        self.decisions.append({"date": day, "signal_date": previous, **weights.to_dict()})
        self.last_week = week
        return decision
