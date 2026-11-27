"""Native Qlib V179 & V182 strategy chain without Qlib or runtime ML training.

Full inheritance chain faithfully preserved from the frozen Qlib originals:
  Band (V48/V50) -> WeakExit (V51) -> Scaled (V61) -> NativeV69 (V69)
  -> NativeV100 (V100: giveback + ridge_confirm + dual_strength, cap 0.375)
  -> NativeV105 (V105: scale=4, gross_limit=0.90)
  -> NativeV125 (V125: defence='recovery', allocation='relative') [Growth sleeve]
  -> NativeV150 (V150: entry_rule='half')
  -> NativeV161 (V161: risk_rule='tail_budget')
  -> NativeV165 (V165: reserve_rule='fixed', limit=0.90)          [Defensive sleeve]
  -> NativeBlend -> NativeV179 (trend) / NativeV182 (market_recovery)

Reuses TICKERS, TARGET, NativeV69, Order, TradeDecisionWO from factorlab.strategies.v69_native.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .qlib_blend_ridge import (
    ModelArtifactError,
    predict_residual_from_artifact,
    select_causal_month_model,
    train_monthly_ridge_artifact,
    validate_ridge_artifact,
)
from .v69_native import NativeV69, Order, TARGET, TICKERS, TradeDecisionWO


def tilt_fixed_budget(weights: pd.Series, score: pd.Series, fraction: float) -> pd.Series:
    """Tilt active, scored sleeves; keep their combined nominal exposure unchanged."""
    active = weights.gt(0) & np.isfinite(score)
    if active.sum() < 2:
        return weights
    w = weights.loc[active]
    s = score.loc[active].clip(-2, 2)
    centered = s - np.average(s, weights=w)
    result = weights.copy()
    result.loc[active] = w * (1 + fraction * centered / max(1, centered.abs().max()))
    return result


class NativeV100(NativeV69):
    """Exact native port of research_v86_v100.policies.Policy + v100.strategy.Strategy."""

    rules = ('giveback', 'ridge_confirm', 'dual_strength')

    def __init__(
        self,
        adjusted_close: pd.DataFrame,
        adjusted_volume: pd.DataFrame,
        adjusted_open: pd.DataFrame | None = None,
        *,
        market_index: pd.Series,
        omit_sndk: bool = False,
        ridge_artifact: dict | None = None,
        allow_offline_fit: bool = False,
        strict_model_month: bool = False,
    ):
        super().__init__(adjusted_close, adjusted_volume, adjusted_open, omit_sndk=omit_sndk)
        if not isinstance(market_index, pd.Series) or market_index.empty:
            raise ValueError('explicit S&P 500 Total Return (^SP500TR) market_index Series required')
        if market_index.index.has_duplicates or not market_index.index.is_monotonic_increasing:
            raise ValueError('unique monotonic market_index session index required')
        self.index = market_index.reindex(self.close.index)
        self.market_fast = self.index.rolling(63).mean()
        self.market_slow = self.index.rolling(126).mean()
        self.breadth_change = self.breadth.diff(10)
        daily = self.close.pct_change(fill_method=None)
        mm = self.index.pct_change(fill_method=None)
        market_var = mm.rolling(63).var().replace(0, np.nan)
        beta = daily.rolling(63).cov(mm).div(market_var, axis=0).clip(-1, 4)
        residual = daily - beta.mul(mm, axis=0)
        self.residual = residual
        self.risk = daily.rolling(42).std().replace(0, np.nan)
        self.relative10 = residual.rolling(10).sum()
        self.score = residual.rolling(42).sum().div(self.risk * np.sqrt(42))
        self.drawdown = self.close.div(self.close.rolling(42).max()).sub(1)
        down = daily.clip(upper=0).pow(2)
        self.downside = down.rolling(42).mean().pow(0.5).replace(0, np.nan)
        self.down_jump = down.rolling(10).mean().div(down.rolling(63).mean().replace(0, np.nan)).pow(0.5)
        market_down = mm.clip(upper=0).pow(2)
        self.market_down_jump = (
            market_down.rolling(10).mean() / market_down.rolling(63).mean().replace(0, np.nan)
        ).pow(0.5)
        self.fastmom = self.close.pct_change(10, fill_method=None)
        self.strict_model_month = strict_model_month

        if {'ridge_tilt', 'ridge_confirm'}.intersection(self.rules):
            names = self.eligible_symbols()
            if ridge_artifact is None:
                if not allow_offline_fit:
                    raise ModelArtifactError(
                        'frozen causal ridge_artifact is required at runtime (offline fit disabled)'
                    )
                ridge_artifact = train_monthly_ridge_artifact(
                    self.close, self.index, omit_sndk=omit_sndk
                )
            else:
                validate_ridge_artifact(ridge_artifact, expected_omit_sndk=omit_sndk)
            self.ridge_artifact = ridge_artifact
            self.forecast = predict_residual_from_artifact(
                self.close[names],
                residual[names],
                self.score[names],
                self.risk[names],
                self.breadth,
                self.index,
                self.ridge_artifact,
                strict_month_coverage=strict_model_month,
            ).reindex(columns=self.close.columns)
        else:
            self.ridge_artifact = None
            self.forecast = pd.DataFrame(np.nan, index=self.close.index, columns=self.close.columns)

    @staticmethod
    def cap(weights: pd.Series) -> pd.Series:
        gross = weights.sum()
        return weights * (0.375 / gross) if gross > 0.375 else weights

    def targets(self, previous, prices, valid_prices, held):
        if self.ridge_artifact is not None:
            select_causal_month_model(
                self.ridge_artifact,
                previous,
                strict_month_coverage=self.strict_model_month,
            )
        weights = super().targets(previous, prices, valid_prices, held)
        for rule in self.rules:
            weights = self.cap(self.apply_rule(rule, previous, prices, held, weights))
        return weights

    def apply_rule(self, rule, t, prices, held, w):
        w = w.copy()
        breadth = self.breadth.at[t]
        market = self.index.at[t]
        rising = market > self.market_slow.at[t]
        weak = breadth < 0.5 and market <= self.market_fast.at[t]
        score = self.score.loc[t]
        active_score = score.where(w.gt(0)).dropna()
        if rule == 'residual_bonus':
            leaders = active_score.loc[active_score.gt(0)].nlargest(2).index
            w = w.mask(score.lt(0), 0.92 * w)
            w.loc[leaders] *= 1.10
        elif rule == 'crash_brake':
            if market < self.market_fast.at[t] and self.market_down_jump.at[t] >= 1.30:
                w *= 0.85
        elif rule == 'giveback':
            weak_stock = held.gt(0) & self.drawdown.loc[t].lt(-0.08) & self.relative10.loc[t].lt(0)
            w = w.mask(weak_stock, 0.88 * w)
        elif rule == 'relative_tilt':
            w = tilt_fixed_budget(w, score, 0.15)
        elif rule == 'weak_entry':
            if weak:
                fading = self.relative10.loc[t].lt(0) & prices.le(self.ma63.loc[t])
                w = w.mask(held.le(0) & fading, 0.0)
        elif rule == 'downside_budget':
            risk = self.downside.loc[t].copy()
            risk['TQQQ'] /= 3
            valid = risk.where(w.gt(0)).dropna()
            if len(valid) >= 3:
                rank = -np.log(risk / valid.median()) * 2
                w = tilt_fixed_budget(w, rank, 0.15)
        elif rule == 'smooth_budget':
            if weak:
                w = w.mask(self.down_jump.loc[t].gt(1.35), 0.9 * w)
            elif rising and breadth >= 2 / 3:
                w = w.mask(self.down_jump.loc[t].lt(1.0), 1.07 * w)
        elif rule == 'ridge_tilt':
            w = tilt_fixed_budget(w, self.forecast.loc[t], 0.12)
        elif rule == 'ridge_confirm':
            low = self.forecast.loc[t].lt(-0.10) & self.high.loc[t] & held.le(0)
            w = w.mask(low, 0.5 * w)
        elif rule == 'size_control':
            w *= 1.05
        elif rule == 'recovery_core':
            if rising and breadth >= 0.5 and self.breadth_change.at[t] >= 0.20 and w['QQQ'] > 0:
                w['QQQ'] += 0.012
        elif rule == 'relative_add':
            if rising and breadth >= 0.5:
                quality = score.gt(0.25) & self.fastmom.loc[t].gt(0)
                w = w.mask(quality, 1.07 * w)
        elif rule == 'concentration_limit':
            name = self.high.loc[t]
            standard = TARGET * 1.10
            oversize = name & w.gt(standard)
            w = w.mask(oversize, standard + 0.40 * (w - standard))
        elif rule == 'short_downside':
            cut = self.down_jump.loc[t].gt(1.35) & prices.lt(self.ma28.loc[t])
            w = w.mask(cut, 0.88 * w)
        elif rule == 'dual_strength':
            if rising and breadth >= 2 / 3:
                leaders = active_score.loc[active_score.gt(0.15)].drop('TQQQ', errors='ignore').nlargest(2).index
                w.loc[leaders] += 0.006
            elif weak:
                w = w.mask(self.down_jump.loc[t].gt(1.20), 0.9 * w)
        else:
            raise ValueError(f'Unregistered rule {rule}')
        return w


class NativeV105(NativeV100):
    """Exact native port of exposure_research.policies.ExposurePolicy + v105.strategy.Strategy."""

    scale = 4
    core = 'none'
    overlay = 'none'
    gross_limit = 0.90

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_200 = self.index.rolling(200).mean()
        self.market_vol = self.index.pct_change(fill_method=None).rolling(21).std() * np.sqrt(252)

    def targets(self, previous, prices, valid_prices, held):
        weights = super().targets(previous, prices, valid_prices, held) * self.scale
        market = self.index.at[previous]
        slow = self.index_200.at[previous]
        strong = market > slow
        breadth = self.breadth.at[previous]
        if self.core == 'static':
            core_weight = 0.20
        elif self.core == 'trend':
            core_weight = 0.20 if strong else 0.0
        elif self.core == 'stepped':
            core_weight = 0.25 if strong else 0.05
        elif self.core == 'breadth':
            core_weight = 0.30 if strong and breadth >= 0.5 else 0.05
        elif self.core == 'volatility':
            volatility = self.market_vol.at[previous]
            core_weight = min(0.30, 0.04 / volatility) if np.isfinite(volatility) and volatility > 0 and strong else 0.05
        elif self.core == 'none':
            core_weight = 0.0
        else:
            raise ValueError(self.core)

        if valid_prices['QQQ'] and np.isfinite(slow):
            weights['QQQ'] += core_weight
        if self.overlay == 'relative':
            weights = tilt_fixed_budget(weights, self.score.loc[previous], 0.25)
        elif self.overlay == 'downside':
            risk = self.downside.loc[previous].replace(0, np.nan)
            ranked = -np.log(risk / risk.where(weights.gt(0)).median())
            weights = tilt_fixed_budget(weights, ranked, 0.35)
        elif self.overlay == 'volguard':
            vol = self.market_vol.at[previous]
            if np.isfinite(vol) and vol > 0.22:
                weights *= max(0.55, 0.22 / vol)
        elif self.overlay == 'joint':
            weights = tilt_fixed_budget(weights, self.score.loc[previous], 0.20)
            if not strong and self.market_down_jump.at[previous] > 1.20:
                weights *= 0.75
        elif self.overlay != 'none':
            raise ValueError(self.overlay)
        gross = weights.sum()
        if gross > self.gross_limit:
            weights *= self.gross_limit / gross
        return weights


class NativeV125(NativeV105):
    """Exact native port of exposure_validation.policies.Refinement + v125.strategy.Strategy."""

    defence = 'recovery'
    allocation = 'relative'

    def __init__(self, adjusted_close, adjusted_volume, adjusted_open=None, **kw):
        super().__init__(adjusted_close, adjusted_volume, adjusted_open, **kw)
        self.market_dd = self.index.div(self.index.rolling(252, min_periods=126).max()).sub(1)
        self.market_trend = self.index.gt(self.index.rolling(126).mean()).rolling(10).mean()
        self.trend_qqq = self.close['QQQ'].div(self.close['QQQ'].rolling(126).mean()).sub(1)
        ids = self.eligible_symbols()
        ret = self.close[ids].pct_change(fill_method=None)
        self.recent_tail = ret.clip(upper=0).pow(2).rolling(21).mean().pow(0.5)

    def targets(self, t, prices, valid_prices, held):
        w = super().targets(t, prices, valid_prices, held).copy()
        dd = self.market_dd.at[t]
        trend = self.market_trend.at[t]
        if self.defence == 'crisis':
            if dd < -0.15 and trend < 0.25:
                w *= 0.55
        elif self.defence == 'smooth':
            if np.isfinite(dd) and np.isfinite(trend):
                severity = np.clip((-dd - 0.06) / 0.20, 0, 1) * (1 - trend)
                w *= 1 - 0.55 * severity
        elif self.defence == 'growth':
            if self.trend_qqq.at[t] < 0 and dd < -0.08:
                w.loc[['QQQ', 'TQQQ']] *= 0.50
        elif self.defence == 'cluster':
            for names, units, limit in [(('JPM', 'C'), (1.0, 1.0), 0.28), (('QQQ', 'TQQQ'), (1.0, 3.0), 0.36)]:
                equivalent = float(np.dot(w.loc[list(names)], units))
                if equivalent > limit:
                    w.loc[list(names)] *= limit / equivalent
        elif self.defence == 'recovery':
            if dd < -0.10 and self.breadth.at[t] < 0.5 and trend < 0.5:
                w *= 0.65
        elif self.defence != 'none':
            raise ValueError(self.defence)

        stocks = w.drop(['QQQ', 'TQQQ'])
        if self.allocation == 'relative':
            w.loc[stocks.index] = tilt_fixed_budget(stocks, self.score.loc[t, stocks.index], 0.30)
        elif self.allocation == 'tail':
            risk = self.recent_tail.loc[t].reindex(stocks.index)
            rank = -np.log(risk / risk.where(stocks.gt(0)).median()).replace([np.inf, -np.inf], np.nan)
            w.loc[stocks.index] = tilt_fixed_budget(stocks, rank, 0.40)
        elif self.allocation != 'none':
            raise ValueError(self.allocation)
        if (w < -1e-12).any() or w.sum() > 0.900000001:
            raise ValueError('Unexpected nominal exposure')
        return w


class NativeV150(NativeV125):
    """Exact native port of entry_research.policies.EntryPolicy + v150.strategy.Strategy."""

    entry_rule = 'half'

    def __init__(self, adjusted_close, adjusted_volume, adjusted_open=None, **kwargs):
        super().__init__(adjusted_close, adjusted_volume, adjusted_open, **kwargs)
        crosses = self.close.gt(self.ma28 * (1 + 0.5 * self.vol))
        ready = self.ma28.notna() & self.vol.notna()
        above = crosses.where(ready)
        self.up3 = above.rolling(3).sum()
        self.up5 = above.rolling(5).sum()
        side = self.close.gt(self.ma28)
        flip = side.ne(side.shift(1))
        self.flips = flip.where(ready & ready.shift(1, fill_value=False)).rolling(21).sum()
        self.extended = self.close.div(self.ma28).sub(1).div(self.vol.replace(0, np.nan))
        lower = self.close.lt(self.ma28 * (1 - 0.5 * self.vol))
        self.down2 = lower & lower.shift(1, fill_value=False)
        self.weak5 = self.close.pct_change(5, fill_method=None).lt(0)

    def targets(self, t, prices, valid_prices, held):
        target = super().targets(t, prices, valid_prices, held)
        new = held.le(0) & target.gt(0)
        if self.entry_rule == 'half':
            return target.mask(new, 0.5 * target)
        raise ValueError(self.entry_rule)


class NativeV161(NativeV150):
    """Exact native port of entry_research.risk.StockRisk + v161.strategy.Strategy."""

    risk_rule = 'tail_budget'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.risk_returns = self.close.pct_change(fill_method=None)
        self.semideviation = self.risk_returns.clip(upper=0).pow(2).rolling(63).mean().pow(0.5)

    def targets(self, t, prices, valid_prices, held):
        w = super().targets(t, prices, valid_prices, held).copy()
        before = w.copy()
        active = w[w.gt(0)].index
        if len(active) < 2:
            return w
        if self.risk_rule == 'tail_budget':
            risk = self.semideviation.loc[t, active] * w.loc[active]
            maximum = risk.sum() * 0.32
            for s in risk[risk.gt(maximum) & risk.gt(0)].index:
                w[s] *= maximum / risk[s]
        else:
            raise ValueError(self.risk_rule)
        freed = before.sum() - w.sum()
        if freed < 1e-12:
            return before
        recipients = w.gt(0) & w.ge(before - 1e-12) & w.lt(0.22)
        recipients['TQQQ'] = False
        headroom = (0.22 - w.loc[recipients]).clip(upper=0.04)
        if headroom.sum() > 0:
            w.loc[recipients] += headroom * min(1.0, freed / headroom.sum())
        if w.sum() > before.sum() + 1e-10 or w.lt(0).any():
            raise ValueError('Concentration overlay increased gross budget')
        return w


class NativeV165(NativeV161):
    """Exact native port of reserve_research.policies.Reserve + v165.strategy.Strategy."""

    reserve_rule = 'fixed'
    limit = 0.90

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_long = self.index.rolling(200).mean()
        self.rebound = self.index.div(self.index.rolling(21).min()).sub(1)
        self.dispersion = self.close[self.eligible_symbols()].pct_change(21, fill_method=None).std(axis=1)
        self.dispersion_mid = self.dispersion.rolling(126).median()

    def targets(self, t, prices, valid_prices, held):
        w = super().targets(t, prices, valid_prices, held)
        total = w.sum()
        if total <= 0 or total >= self.limit - 1e-10:
            return w
        room = self.limit - total
        mode = self.reserve_rule
        if mode == 'fixed':
            add = min(room, 0.10 * total)
        else:
            raise ValueError(mode)
        if add <= 0:
            return w
        eligible = w.gt(0)
        recipients = w.where(eligible, 0.0)
        if recipients.sum() <= 0:
            return w
        extra = recipients * (add / recipients.sum())
        extra = extra.clip(upper=0.04)
        extra['TQQQ'] = min(extra['TQQQ'], 0.012)
        out = w + extra
        if out.sum() > self.limit + 1e-10 or out.lt(0).any():
            raise ValueError('Reserve exceeded')
        return out


class NativeBlend(NativeV165):
    """Exact native port of mix_research.policies.Blend."""

    blend_rule = 'half'

    def blend_share(self, t, growth: pd.Series) -> float:
        rule = self.blend_rule
        if rule == 'trend':
            return 0.75 if self.index.at[t] > self.index_long.at[t] and self.breadth.at[t] >= 0.5 else 0.25
        if rule == 'market_recovery':
            return 0.75 if self.breadth_change.at[t] > 0.10 and self.index.at[t] > self.market_fast.at[t] else 0.40
        raise ValueError(rule)

    def targets(self, t, prices, valid_prices, held):
        growth = NativeV125.targets(self, t, prices, valid_prices, held)
        defensive = super().targets(t, prices, valid_prices, held)
        share = self.blend_share(t, growth)
        allocation = growth * share + defensive * (1 - share)
        if not np.isfinite(allocation).all() or allocation.lt(0).any() or allocation.sum() > 0.90000001:
            raise ValueError('Blended exposure invariant failed')
        return allocation

    def explain_targets(self, t, prices, valid_prices, held) -> dict:
        """Return full intermediate decomposition for differential auditing and P10-04 research."""
        w_v69 = NativeV69.targets(self, t, prices, valid_prices, held)
        w_v100 = NativeV100.targets(self, t, prices, valid_prices, held)
        w_v105 = NativeV105.targets(self, t, prices, valid_prices, held)
        growth = NativeV125.targets(self, t, prices, valid_prices, held)
        w_v150 = NativeV150.targets(self, t, prices, valid_prices, held)
        w_v161 = NativeV161.targets(self, t, prices, valid_prices, held)
        defensive = NativeV165.targets(self, t, prices, valid_prices, held)
        share = self.blend_share(t, growth)
        allocation = growth * share + defensive * (1 - share)
        if not np.isfinite(allocation).all() or allocation.lt(0).any() or allocation.sum() > 0.90000001:
            raise ValueError('Blended exposure invariant failed')
        month_key, entry = (
            select_causal_month_model(self.ridge_artifact, t, strict_month_coverage=self.strict_model_month)
            if self.ridge_artifact is not None else (None, None)
        )
        return {
            'signal_date': str(pd.Timestamp(t).date()),
            'blend_rule': self.blend_rule,
            'blend_share': float(share),
            'defensive_share': float(1.0 - share),
            'breadth': float(self.breadth.at[t]) if np.isfinite(self.breadth.at[t]) else None,
            'breadth_change': float(self.breadth_change.at[t]) if np.isfinite(self.breadth_change.at[t]) else None,
            'index_close': float(self.index.at[t]) if np.isfinite(self.index.at[t]) else None,
            'index_ma63': float(self.market_fast.at[t]) if np.isfinite(self.market_fast.at[t]) else None,
            'index_ma126': float(self.market_slow.at[t]) if np.isfinite(self.market_slow.at[t]) else None,
            'index_ma200': float(self.index_long.at[t]) if np.isfinite(self.index_long.at[t]) else None,
            'market_dd': float(self.market_dd.at[t]) if np.isfinite(self.market_dd.at[t]) else None,
            'market_trend': float(self.market_trend.at[t]) if np.isfinite(self.market_trend.at[t]) else None,
            'ridge_month': month_key,
            'ridge_train_cutoff': None if entry is None else entry['train_cutoff'],
            'ridge_forecast': {
                s: (None if not np.isfinite(self.forecast.at[t, s]) else float(self.forecast.at[t, s]))
                for s in TICKERS
            },
            'v69_weights': {s: float(w_v69[s]) for s in TICKERS},
            'v100_weights': {s: float(w_v100[s]) for s in TICKERS},
            'v105_weights': {s: float(w_v105[s]) for s in TICKERS},
            'growth_v125_weights': {s: float(growth[s]) for s in TICKERS},
            'v150_weights': {s: float(w_v150[s]) for s in TICKERS},
            'v161_weights': {s: float(w_v161[s]) for s in TICKERS},
            'defensive_v165_weights': {s: float(defensive[s]) for s in TICKERS},
            'final_weights': {s: float(allocation[s]) for s in TICKERS},
        }


class NativeV179(NativeBlend):
    blend_rule = 'trend'


class NativeV182(NativeBlend):
    blend_rule = 'market_recovery'


class _BlendPlannerMixin:
    """Exact research-unit weekly order generation from V50Strategy.generate_trade_decision."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_week = None
        self.decisions = []
        self.diagnostics = []

    def generate_trade_decision(self, *, record_diagnostics: bool = False):
        start, end = self.trade_calendar.get_step_time()
        day = start.normalize()
        iso = day.isocalendar()
        week = (iso.year, iso.week)
        if week == self.last_week:
            return TradeDecisionWO([], self)
        i = self.close.index.get_loc(day)
        if i == 0:
            raise ValueError('回测起点前至少需要一个交易日的行情')
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
        if record_diagnostics:
            self.diagnostics.append(self.explain_targets(previous, prices, valid, held))
        held_value = (held * prices.where(tradable)).fillna(0.0)
        nav = float(position.get_cash() + held_value.sum())
        if not np.isfinite(nav) or nav <= 0:
            raise ValueError('Invalid account NAV')
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
        self.decisions.append({'date': day, 'signal_date': previous, **weights.to_dict()})
        self.last_week = week
        return decision


class V179Planner(_BlendPlannerMixin, NativeV179):
    pass


class V182Planner(_BlendPlannerMixin, NativeV182):
    pass


class NativeBlendSignalAdapter:
    """SignalGenerator adapter exposing native FactorLab V179/V182 to P10-04 independent validation."""

    signal_source = 'NATIVE_FL'

    def __init__(
        self,
        version: str,
        close: pd.DataFrame,
        volume: pd.DataFrame,
        opening: pd.DataFrame | None = None,
        *,
        market_index: pd.Series,
        omit_sndk: bool = False,
        omit_symbols: tuple[str, ...] = (),
        blend_override: tuple[float, float] | None = None,
        ridge_artifact: dict | None = None,
    ) -> None:
        key = version.lower()
        if key == 'v179':
            cls = NativeV179
        elif key == 'v182':
            cls = NativeV182
        else:
            raise ValueError(f'Unsupported native blend strategy version: {version}')
        self.strategy_name = key
        self._omit_symbols = tuple(sorted(set(omit_symbols) | ({'SNDK'} if omit_sndk else set())))
        self._blend_override = blend_override
        self._strategy = cls(
            close,
            volume,
            opening,
            market_index=market_index,
            omit_sndk=omit_sndk,
            ridge_artifact=ridge_artifact,
            allow_offline_fit=(ridge_artifact is None),
            strict_model_month=False,
        )

    def targets(
        self,
        previous: pd.Timestamp,
        prices: pd.Series,
        valid_prices: pd.Series,
        held: pd.Series,
    ) -> pd.Series:
        if self._blend_override is not None:
            high_share, low_share = self._blend_override
            growth = NativeV125.targets(self._strategy, previous, prices, valid_prices, held)
            defensive = NativeV165.targets(self._strategy, previous, prices, valid_prices, held)
            if self._strategy.blend_rule == 'trend':
                cond = (
                    self._strategy.index.at[previous] > self._strategy.index_long.at[previous]
                    and self._strategy.breadth.at[previous] >= 0.5
                )
            else:
                cond = (
                    self._strategy.breadth_change.at[previous] > 0.10
                    and self._strategy.index.at[previous] > self._strategy.market_fast.at[previous]
                )
            share = high_share if cond else low_share
            w = (growth * share + defensive * (1.0 - share)).copy()
        else:
            w = self._strategy.targets(previous, prices, valid_prices, held).copy()
        for sym in self._omit_symbols:
            if sym in w.index:
                w.loc[sym] = 0.0
        return w

    def indicator_snapshot(self, previous: pd.Timestamp) -> dict:
        s = self._strategy
        return {
            'breadth': float(s.breadth.at[previous]) if pd.notna(s.breadth.at[previous]) else None,
            'index': float(s.index.at[previous]) if pd.notna(s.index.at[previous]) else None,
            'index_long_200': float(s.index_long.at[previous]) if pd.notna(s.index_long.at[previous]) else None,
            'market_fast_63': float(s.market_fast.at[previous]) if pd.notna(s.market_fast.at[previous]) else None,
            'breadth_change_10': float(s.breadth_change.at[previous]) if pd.notna(s.breadth_change.at[previous]) else None,
        }


def build_signal_provider(
    version: str,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    opening: pd.DataFrame | None = None,
    *,
    market_index: pd.Series,
    omit_sndk: bool = False,
    omit_symbols: tuple[str, ...] = (),
    blend_override: tuple[float, float] | None = None,
    ridge_artifact: dict | None = None,
) -> NativeBlendSignalAdapter:
    return NativeBlendSignalAdapter(
        version,
        close,
        volume,
        opening,
        market_index=market_index,
        omit_sndk=omit_sndk,
        omit_symbols=omit_symbols,
        blend_override=blend_override,
        ridge_artifact=ridge_artifact,
    )
