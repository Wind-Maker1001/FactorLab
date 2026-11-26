"""Stateless native V179 and V182 portfolio boundary with explicit JSON checkpoints."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from .qlib_blend_ridge import validate_ridge_artifact
from .v179_v182_native import V179Planner, V182Planner
from .v69 import ASSETS, first_session_of_week
from .v69_alpaca import original_plan_quantities, to_adjusted_holdings

BENCHMARK_SERIES = '^SP500TR'
DISALLOWED_BENCHMARK_SUBSTITUTES = frozenset({'SPY', 'QQQ', 'IVV', 'VOO', '^GSPC', 'SPX', '^SPX'})


def validate_market_index(market_index: pd.Series, previous: pd.Timestamp, *, series_id: str = BENCHMARK_SERIES):
    """Enforce explicit ^SP500TR read-only benchmark identity and strict causal cutoff."""
    if series_id != BENCHMARK_SERIES or series_id in DISALLOWED_BENCHMARK_SUBSTITUTES:
        raise ValueError(f'only read-only {BENCHMARK_SERIES} benchmark is allowed (got {series_id!r})')
    if not isinstance(market_index, pd.Series) or market_index.empty:
        raise ValueError('nonempty ^SP500TR market_index Series required')
    if market_index.index.has_duplicates or not market_index.index.is_monotonic_increasing:
        raise ValueError('unique monotonic market_index session index required')
    if market_index.index[-1] != previous:
        raise ValueError('market_index must end at preceding completed session')
    last_val = market_index.at[previous]
    if not np.isfinite(last_val) or last_val <= 0:
        raise ValueError('finite positive ^SP500TR close required at signal session')
    return market_index


class _NativeBlendStrategy:
    """Stateless weekly preparation with explicit JSON checkpoint and frozen Ridge artifact."""

    planner_cls = None
    strategy_name = ''

    def __init__(
        self,
        ridge_artifact: dict | None = None,
        *,
        omit_sndk: bool = False,
        strict_model_month: bool = True,
        allow_offline_fit: bool = False,
    ):
        if ridge_artifact is not None:
            validate_ridge_artifact(ridge_artifact, expected_omit_sndk=omit_sndk)
        elif not allow_offline_fit:
            raise ValueError('frozen causal ridge_artifact required unless allow_offline_fit=True')
        self.ridge_artifact = ridge_artifact
        self.omit_sndk = bool(omit_sndk)
        self.strict_model_month = bool(strict_model_month)
        self.allow_offline_fit = bool(allow_offline_fit)

    def decide(self, event: dict, state: dict, *, include_diagnostics: bool = True) -> dict:
        day = pd.Timestamp(event['session'])
        calendar = event['calendar']
        if not first_session_of_week(calendar, day):
            return dict(weights=None, orders=[], diagnostics=None, state=dict(state))
        week = list(day.isocalendar()[:2])
        if state.get('last_week') == week:
            return dict(weights=None, orders=[], diagnostics=None, state=dict(state))
        previous = calendar.shift(day, -1)
        close, volume = event['close'], event['volume']
        if close.index[-1] != previous or volume.index[-1] != previous:
            raise ValueError('history must end at preceding completed session')
        series_id = event.get('benchmark_series', BENCHMARK_SERIES)
        market_index = validate_market_index(event['market_index'], previous, series_id=series_id)

        # Append only an empty current-session label, never a current-session observation.
        extended_index = close.index.append(pd.DatetimeIndex([day]))
        engine = self.planner_cls(
            close.reindex(extended_index),
            volume.reindex(extended_index),
            market_index=market_index.reindex(extended_index),
            omit_sndk=self.omit_sndk,
            ridge_artifact=self.ridge_artifact,
            allow_offline_fit=self.allow_offline_fit,
            strict_model_month=self.strict_model_month,
        )
        engine.trade_calendar = SimpleNamespace(get_step_time=lambda: (day, day))
        engine.trade_position = SimpleNamespace(
            get_stock_amount_dict=lambda: dict(event['holdings']),
            get_cash=lambda: float(event['cash']),
        )
        decision = engine.generate_trade_decision(record_diagnostics=include_diagnostics)
        weights = {k: float(engine.decisions[-1][k]) for k in ASSETS}
        diagnostics = engine.diagnostics[-1] if include_diagnostics and engine.diagnostics else None
        next_state = {
            'last_week': week,
            'last_signal_date': str(previous.date()),
            'last_execution_session': str(day.date()),
            'last_blend_share': None if diagnostics is None else diagnostics['blend_share'],
            'last_ridge_month': None if diagnostics is None else diagnostics['ridge_month'],
            'last_ridge_cutoff': None if diagnostics is None else diagnostics['ridge_train_cutoff'],
            'model_artifact_sha256': (
                None if engine.ridge_artifact is None else engine.ridge_artifact.get('artifact_sha256')
            ),
        }
        return dict(
            weights=weights,
            orders=[
                dict(
                    symbol=o.stock_id,
                    side='sell' if o.direction == 0 else 'buy',
                    adjusted_quantity=float(o.amount),
                )
                for o in decision.order_list
            ],
            diagnostics=diagnostics,
            state=next_state,
        )


class V179Strategy(_NativeBlendStrategy):
    planner_cls = V179Planner
    strategy_name = 'V179_TREND_BLEND'


class V182Strategy(_NativeBlendStrategy):
    planner_cls = V182Planner
    strategy_name = 'V182_RECOVERY_BLEND'


__all__ = [
    'ASSETS',
    'BENCHMARK_SERIES',
    'DISALLOWED_BENCHMARK_SUBSTITUTES',
    'V179Strategy',
    'V182Strategy',
    'validate_market_index',
    'to_adjusted_holdings',
    'original_plan_quantities',
]
