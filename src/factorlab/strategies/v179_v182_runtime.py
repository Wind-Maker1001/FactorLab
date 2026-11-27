"""UnifiedStrategyV2 entrypoints and packaging builders for native V179 & V182."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal as D
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.trading.contracts import Market
from .package import build_package, model_refresh_candidate
from .protocol import DecisionV2, StrategySpecV2
from .qlib_blend_ridge import dump_artifact_python_module, validate_ridge_artifact
from .v179_v182 import (
    ASSETS,
    BENCHMARK_SERIES,
    DISALLOWED_BENCHMARK_SUBSTITUTES,
    V179Strategy,
    V182Strategy,
)
from .v69_alpaca import original_plan_quantities, to_adjusted_holdings


class _AlpacaBlendStrategyV2:
    """Shared UnifiedStrategyV2 implementation for V179 and V182 over Alpaca US equities.

    Design guarantees:
    - Uses ONLY actual instance cash/holdings from event['allocation'], never a hardcoded $300k.
    - Separates the 8 tradable US equities (spec.symbols) from read-only ^SP500TR benchmark data
      (event['context']['benchmark']['^SP500TR']).
    - Never performs ML training at runtime; loads a frozen causal monthly Ridge artifact.
    - Supports SNDK pre-listing/warmup leading NaNs without breaking the 7 active symbols.
    - Persists and restores weekly rebalance and model state via DecisionV2.state so restarts,
      pauses, resumes and package upgrades never trigger duplicate weekly rebalances.
    """

    strategy_id: str = ''
    strategy_version: str = ''
    state_schema_version: str = ''
    strategy_cls = None

    def __init__(
        self,
        account_id: str,
        capital: str = '0',
        *,
        omit_sndk: bool = False,
        strict_model_month: bool = True,
        model_artifact: dict | None = None,
        feed: str | None = None,
        history_limit: int = 252,
    ):
        self.account_id = account_id
        self.capital = D(str(capital))
        self.omit_sndk = bool(omit_sndk)
        self.strict_model_month = bool(strict_model_month)
        self.feed = feed
        self.history_limit = int(history_limit)
        self._model_artifact = model_artifact

    def _load_model_artifact(self) -> dict:
        if self._model_artifact is not None:
            return validate_ridge_artifact(self._model_artifact, expected_omit_sndk=self.omit_sndk)
        # Allow a strategy package to bundle its frozen artifact in sibling `model_artifact.py`.
        try:
            import model_artifact as bundled  # type: ignore
            return validate_ridge_artifact(bundled.MODEL_ARTIFACT, expected_omit_sndk=self.omit_sndk)
        except ImportError as exc:
            raise ValueError('frozen causal monthly Ridge model_artifact is required') from exc

    def describe(self) -> StrategySpecV2:
        data_cfg = dict(
            data_types=['closed_bar', 'quote'],
            history_limit=self.history_limit,
            historical_observations=False,
            adjustment_basis='yahoo_ratio',
            benchmark_series=BENCHMARK_SERIES,
            allow_partial_symbol_history=['SNDK'],
        )
        if self.feed is not None:
            data_cfg['feed'] = self.feed
        return StrategySpecV2(
            strategy_id=self.strategy_id,
            version=self.strategy_version,
            market=Market.ALPACA_US_EQUITY.value,
            account_id=self.account_id,
            symbols=list(ASSETS),
            decision_kind='quantity_deltas',
            data=data_cfg,
            schedule=dict(
                kind='calendar',
                calendar_id='alpaca',
                timezone='America/New_York',
                event='natural_week_open',
            ),
            state=dict(
                version=self.state_schema_version,
                migration='preserve',
                from_versions=[],
            ),
            capital=dict(currency='USD', requested=self.capital),
            execution=dict(
                order_types=['LIMIT'],
                time_in_force=['DAY'],
                shorting=False,
                fractional_quantities=True,
                reduce_only=False,
                economic_multipliers={'TQQQ': '3'},
            ),
        )

    def migrate_state(self, state: dict, from_version: str) -> dict:
        if from_version != self.state_schema_version:
            raise ValueError(f'unsupported state migration from {from_version} to {self.state_schema_version}')
        return json.loads(json.dumps(state, allow_nan=False))

    def _extract_benchmark(self, event: dict, expected_index: pd.DatetimeIndex) -> pd.Series:
        context = event.get('context') or {}
        benchmarks = context.get('benchmark')
        if not isinstance(benchmarks, dict) or BENCHMARK_SERIES not in benchmarks:
            raise ValueError(f'missing read-only benchmark {BENCHMARK_SERIES} in event context')
        bundle = benchmarks[BENCHMARK_SERIES]
        series_id = bundle.get('series_id')
        if series_id != BENCHMARK_SERIES or series_id in DISALLOWED_BENCHMARK_SUBSTITUTES:
            raise ValueError(f'invalid benchmark series_id: {series_id!r}')
        records = bundle.get('history')
        if not isinstance(records, list) or not records:
            raise ValueError(f'empty benchmark history for {BENCHMARK_SERIES}')
        event_time = pd.Timestamp(event['event_time'])
        signal_cutoff = bundle.get('signal_cutoff')
        cutoff_ts = None if signal_cutoff is None else pd.Timestamp(signal_cutoff)
        values = {}
        for row in records:
            obs_at = pd.Timestamp(row['observed_at'])
            eff_at = pd.Timestamp(row['effective_at'])
            if eff_at > obs_at or obs_at > event_time or eff_at > event_time:
                raise ValueError('noncausal benchmark observation detected')
            if cutoff_ts is not None and eff_at > cutoff_ts:
                raise ValueError('benchmark observation exceeds signal_cutoff')
            payload = row['payload']
            session = pd.Timestamp(payload['session']).normalize()
            close_val = float(payload['close'])
            if not np.isfinite(close_val) or close_val <= 0:
                raise ValueError(f'invalid benchmark close at {session.date()}')
            values[session] = close_val
        series = pd.Series(values, dtype=float).sort_index()
        aligned = series.reindex(expected_index)
        if aligned.isna().any():
            raise ValueError(f'incomplete {BENCHMARK_SERIES} benchmark coverage over required sessions')
        return aligned

    def decide(self, event: dict, state: dict) -> DecisionV2:
        from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar

        context = event.get('context') or {}
        session = context['execution_session']
        calendar = AlpacaSessionCalendar(
            context['calendar'],
            coverage_start=context['coverage_start'],
            coverage_end=context['coverage_end'],
        )
        checkpoint = dict(state.get('checkpoint', state)) if isinstance(state, dict) else {}

        # Extract per-symbol adjusted close/volume history (allowing leading pre-listing NaNs for SNDK).
        close_cols = {}
        vol_cols = {}
        all_dates = set()
        for symbol in ASSETS:
            rows = [entry['payload'] for entry in event['symbols'][symbol]['history']['closed_bar']]
            c_map = {}
            v_map = {}
            for row in rows:
                day = pd.Timestamp(row['session']).normalize()
                c_val = row.get('adjusted_close', row.get('close'))
                v_val = row.get('adjusted_volume', row.get('volume'))
                c_map[day] = float('nan') if c_val is None else float(c_val)
                v_map[day] = float('nan') if v_val is None else float(v_val)
                all_dates.add(day)
            close_cols[symbol] = pd.Series(c_map, dtype=float)
            vol_cols[symbol] = pd.Series(v_map, dtype=float)

        if not all_dates:
            raise ValueError('empty closed_bar history')
        session_index = pd.DatetimeIndex(sorted(all_dates))
        close = pd.DataFrame(close_cols).reindex(index=session_index, columns=list(ASSETS))
        volume = pd.DataFrame(vol_cols).reindex(index=session_index, columns=list(ASSETS))

        # Non-SNDK symbols must have complete finite history; SNDK may be NaN pre-listing.
        non_sndk = [s for s in ASSETS if s != 'SNDK']
        if close[non_sndk].isna().any().any() or volume[non_sndk].isna().any().any():
            raise ValueError('incomplete non-SNDK adjusted close/volume history')
        if len(session_index) < self.history_limit:
            raise ValueError(f'expected at least {self.history_limit} completed sessions, got {len(session_index)}')

        market_index = self._extract_benchmark(event, session_index)
        raw_holdings = {s: D(str(event['allocation'].get('positions', {}).get(s, '0'))) for s in ASSETS}
        factors = {s: D(str(context['factors'][s])) for s in ASSETS}
        steps = {s: D(str(context['quantity_steps'][s])) for s in ASSETS}
        cash = float(D(str(event['allocation']['cash'])))

        artifact = self._load_model_artifact()
        strategy = self.strategy_cls(
            artifact,
            omit_sndk=self.omit_sndk,
            strict_model_month=self.strict_model_month,
            allow_offline_fit=False,
        )
        decision = strategy.decide(
            dict(
                session=session,
                calendar=calendar,
                close=close,
                volume=volume,
                market_index=market_index,
                benchmark_series=BENCHMARK_SERIES,
                holdings=to_adjusted_holdings(raw_holdings, factors),
                cash=cash,
            ),
            checkpoint,
            include_diagnostics=True,
        )

        if decision['weights'] is None:
            next_state = dict(state) if isinstance(state, dict) else {}
            next_state['checkpoint'] = decision['state']
            return DecisionV2('quantity_deltas', targets={}, state=next_state)

        _, deltas = original_plan_quantities(
            decision,
            raw_holdings=raw_holdings,
            factors=factors,
            quantity_steps=steps,
        )
        return DecisionV2(
            'quantity_deltas',
            targets=deltas,
            state=dict(
                checkpoint=decision['state'],
                last_weights=decision['weights'],
                last_diagnostics=decision['diagnostics'],
            ),
        )


class V179AlpacaStrategy(_AlpacaBlendStrategyV2):
    strategy_id = 'V179_TREND_BLEND'
    strategy_version = '179.1'
    state_schema_version = 'v179-native-1'
    strategy_cls = V179Strategy


class V182AlpacaStrategy(_AlpacaBlendStrategyV2):
    strategy_id = 'V182_RECOVERY_BLEND'
    strategy_version = '182.1'
    state_schema_version = 'v182-native-1'
    strategy_cls = V182Strategy


def build_blend_strategy_package(
    *,
    variant: str,
    account_id: str,
    capital: str,
    model_artifact: dict,
    code_commit: str,
    destination: Path | str,
    evidence: dict,
    omit_sndk: bool = False,
    strict_model_month: bool = True,
    feed: str | None = None,
    history_limit: int = 252,
    migration: str = 'artifact_refresh',
    staging_dir: Path | str | None = None,
) -> dict:
    """Build a content-addressed V2 StrategyPackage zip containing strategy.py + model_artifact.py."""
    import tempfile

    key = variant.lower()
    if key == 'v179':
        cls_name = 'V179AlpacaStrategy'
        cls = V179AlpacaStrategy
    elif key == 'v182':
        cls_name = 'V182AlpacaStrategy'
        cls = V182AlpacaStrategy
    else:
        raise ValueError(f'unknown blend strategy variant: {variant}')

    validated_artifact = validate_ridge_artifact(model_artifact, expected_omit_sndk=omit_sndk)
    config = dict(
        account_id=account_id,
        capital=str(capital),
        omit_sndk=bool(omit_sndk),
        strict_model_month=bool(strict_model_month),
        feed=feed,
        history_limit=int(history_limit),
    )
    declaration = cls(**config, model_artifact=validated_artifact).describe()
    fields = asdict(declaration)
    requirements = {k: fields[k] for k in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital']['requested'] = str(declaration.capital['requested'])

    entry_source = (
        f'"""Packaged native {variant.upper()} UnifiedStrategyV2 entrypoint."""\n'
        f'from factorlab.strategies.v179_v182_runtime import {cls_name}\n'
        'from .model_artifact import MODEL_ARTIFACT\n\n\n'
        f'class Strategy({cls_name}):\n'
        '    def __init__(self, *args, **kwargs):\n'
        '        kwargs.setdefault("model_artifact", MODEL_ARTIFACT)\n'
        '        super().__init__(*args, **kwargs)\n'
    )
    artifact_source = dump_artifact_python_module(validated_artifact)

    manifest = dict(
        schema_version=2,
        interface='FACTORLAB_DECISION_V2',
        package=True,
        strategy_id=declaration.strategy_id,
        strategy_version=declaration.version,
        market=declaration.market,
        account_id=account_id,
        symbols=list(ASSETS),
        execution_mode=declaration.decision_kind,
        requirements=requirements,
        code_commit=code_commit,
        entrypoint='strategy:Strategy',
        files={'strategy.py': '', 'model_artifact.py': ''},
        configuration=config,
        dependencies=[],
        evidence=dict(evidence, model_artifact_sha256=validated_artifact['artifact_sha256']),
        model_artifact=dict(
            kind=validated_artifact['kind'],
            schema_version=validated_artifact['schema_version'],
            file='model_artifact.py',
            artifact_sha256=validated_artifact['artifact_sha256'],
            covered_start=validated_artifact['covered_start'],
            covered_end=validated_artifact['covered_end'],
            omit_sndk=bool(validated_artifact['omit_sndk']),
            fitted_months=len(validated_artifact['models']),
        ),
        migration=migration,
    )

    if staging_dir is not None:
        source = Path(staging_dir)
        source.mkdir(parents=True, exist_ok=True)
        (source / 'strategy.py').write_bytes(entry_source.encode('utf-8'))
        (source / 'model_artifact.py').write_bytes(artifact_source.encode('utf-8'))
        return build_package(source, manifest, Path(destination))

    with tempfile.TemporaryDirectory(prefix=f'factorlab-{key}-pkg-') as tmp:
        source = Path(tmp)
        (source / 'strategy.py').write_bytes(entry_source.encode('utf-8'))
        (source / 'model_artifact.py').write_bytes(artifact_source.encode('utf-8'))
        return build_package(source, manifest, Path(destination))


def build_v179_package(**kwargs) -> dict:
    return build_blend_strategy_package(variant='v179', **kwargs)


def build_v182_package(**kwargs) -> dict:
    return build_blend_strategy_package(variant='v182', **kwargs)


__all__ = [
    'V179AlpacaStrategy',
    'V182AlpacaStrategy',
    'build_blend_strategy_package',
    'build_v179_package',
    'build_v182_package',
    'model_refresh_candidate',
]
