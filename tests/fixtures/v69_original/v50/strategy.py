"""Qlib weekly US trend strategy. Default rule is the development-window selection."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, TradeDecisionWO
from v48_strategy import ROOT, TARGET, TICKERS, V48Strategy

VERSION = Path(__file__).resolve().parent


def histories(end=None):
    bars = {s: pd.read_parquet(ROOT / "data" / f"{s}.parquet").loc[:end] for s in TICKERS}
    close = pd.DataFrame({s: x.adjusted for s, x in bars.items()})
    volume = pd.DataFrame({s: x.volume / x.adjust_ratio for s, x in bars.items()})
    opening = pd.DataFrame({s: x.open * x.adjust_ratio for s, x in bars.items()})
    return close, volume, opening


def monthly_predictions(close, opening, vol, ma28, ma63, breadth):
    from xgboost import XGBRegressor

    ret5 = close.pct_change(5, fill_method=None)
    ret21 = close.pct_change(21, fill_method=None)
    forward = opening.shift(-6) / opening.shift(-1) - 1
    parts = []
    for symbol in close:
        parts.append(pd.DataFrame({
            "symbol": symbol, "dist28": close[symbol] / ma28[symbol] - 1,
            "dist63": close[symbol] / ma63[symbol] - 1, "ret5": ret5[symbol],
            "relative21": ret21[symbol] - ret21.QQQ, "vol21": vol[symbol],
            "market21": ret21.QQQ, "breadth": breadth, "label": forward[symbol],
        }))
    panel = pd.concat(parts).sort_index().replace([np.inf, -np.inf], np.nan)
    features = [k for k in panel if k not in ("symbol", "label")]
    panel = panel.dropna(subset=features)
    predictions = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    months = close.index.to_period("M")
    for month in months.unique():
        days = close.index[months == month]
        i = close.index.get_loc(days[0])
        if i < 6:
            continue
        # At first signal close of month, open at feature date + 6 is already known.
        training = panel.loc[close.index[max(0, i - 6 - 503)]:close.index[i - 6]].dropna(subset=["label"])
        query = panel.loc[days[0]:days[-1]]
        if training.index.nunique() < 126 or query.empty:
            continue
        model = XGBRegressor(n_estimators=45, max_depth=2, learning_rate=0.05,
                             reg_lambda=10, min_child_weight=20, random_state=49,
                             objective="reg:squarederror", n_jobs=1)
        model.fit(training[features], training.label)
        scored = query[["symbol"]].copy()
        scored["value"] = model.predict(query[features])
        wide = scored.set_index("symbol", append=True)["value"].unstack()
        predictions.loc[wide.index, wide.columns] = wide
    return predictions


class V50Strategy(V48Strategy):
    def __init__(self, adjusted_close=None, adjusted_volume=None, adjusted_open=None,
                 *, rule=None, omit_sndk=False):
        if adjusted_close is None:
            adjusted_close, adjusted_volume, adjusted_open = histories()
        if rule is None:
            rule = json.loads((VERSION / "selection.json").read_text(encoding="utf-8"))["selected"]
        if rule not in {"band", "blend", "band_xgb", "reference"}:
            raise ValueError(f"Unknown rule: {rule}")
        super().__init__(adjusted_close, adjusted_volume)
        self.rule = rule
        self.omit_sndk = omit_sndk
        self.ma63 = self.close.rolling(63).mean()
        self.vol = self.close.pct_change(fill_method=None).rolling(21).std()
        eligible = [s for s in TICKERS if s != "SNDK" or not omit_sndk]
        ready = self.ma28[eligible].notna() & self.close[eligible].gt(0) & np.isfinite(self.close[eligible])
        self.breadth = (self.close[eligible].gt(self.ma28[eligible]) & ready).sum(axis=1) / ready.sum(axis=1).replace(0, np.nan)
        if rule == "band_xgb":
            self.predictions = monthly_predictions(self.close[eligible], adjusted_open[eligible],
                self.vol[eligible], self.ma28[eligible], self.ma63[eligible], self.breadth)
        self.decisions = []

    def targets(self, previous, prices, valid_prices, held):
        fast = self.ma28.loc[previous]
        slow = self.ma63.loc[previous]
        above = prices.gt(fast)
        qqq_on = valid_prices["QQQ"] and prices["QQQ"] > self.qqq_ma63.at[previous]
        if self.rule == "reference":
            weights = TARGET.where(above, 0.0)
        else:
            if self.rule == "blend":
                weights = TARGET * (2 * above.astype(float) + prices.gt(slow).astype(float)) / 3
                weights = weights.where(fast.notna(), 0.0)
            else:
                bandwidth = 0.5 * self.vol.loc[previous]
                enter = prices.gt(fast * (1 + bandwidth))
                stay = held.gt(0) & prices.ge(fast * (1 - bandwidth))
                weights = TARGET.where(enter | stay, 0.0)
                if self.rule == "band_xgb":
                    forecast = self.predictions.loc[previous].reindex(TICKERS)
                    scale = np.sqrt(5) * self.vol.loc[previous].replace(0, np.nan)
                    weights *= 1 + 0.15 * np.tanh(forecast / scale).fillna(0.0)
            if qqq_on and self.breadth.at[previous] >= 2 / 3:
                weights *= 1.10
        if not qqq_on:
            weights.loc[["QQQ", "TQQQ"]] = 0.0
        if self.omit_sndk:
            weights["SNDK"] = 0.0
        return weights.where(valid_prices, 0.0)

    def generate_trade_decision(self, execute_result=None):
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
