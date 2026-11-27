"""Offline monthly Ridge trainer and zero-sklearn causal online inference.

Preserves the exact mathematical semantics of research_v86_v100.policies.predict_residual
while cleanly separating offline model training (research side, uses scikit-learn) from
online causal inference (runtime/cloud side, pure NumPy/Pandas only, never imports sklearn).
"""
from __future__ import annotations

import ast
from hashlib import sha256
import json
from typing import Mapping

import numpy as np
import pandas as pd

SCHEMA_VERSION = 1
ARTIFACT_KIND = 'qlib_v100_monthly_ridge'
FEATURE_NAMES = ('momentum', 'trend', 'relative5', 'risk', 'breadth', 'market21')
RIDGE_ALPHA = 80.0
MIN_TRAIN_DAYS = 126
TRAIN_LOOKBACK_SESSIONS = 508
LABEL_MATURITY_SESSIONS = 5


class ModelArtifactError(ValueError):
    """Raised when a frozen monthly Ridge artifact fails schema or causality validation."""


class ModelArtifactStaleError(ModelArtifactError):
    """Raised when online inference requests a signal month beyond covered artifact months."""


def canonical_artifact_digest(artifact: Mapping) -> str:
    payload = {k: v for k, v in artifact.items() if k != 'artifact_sha256'}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return sha256(raw).hexdigest()


def compute_v100_ridge_features(
    close: pd.DataFrame,
    residual: pd.DataFrame,
    score: pd.DataFrame,
    risk: pd.DataFrame,
    breadth: pd.Series,
    market: pd.Series,
) -> pd.DataFrame:
    """Build the causal six-feature panel matching research_v86_v100.policies.predict_residual."""
    market21 = market.pct_change(21, fill_method=None)
    blocks = []
    for symbol in close.columns:
        trend = close[symbol].div(close[symbol].rolling(28).mean()).sub(1)
        blocks.append(pd.DataFrame({
            'ticker': symbol,
            'momentum': score[symbol],
            'trend': trend,
            'relative5': residual[symbol].rolling(5).sum(),
            'risk': risk[symbol],
            'breadth': breadth,
            'market21': market21,
        }))
    panel = pd.concat(blocks).sort_index().replace([np.inf, -np.inf], np.nan)
    return panel


def _compute_base_series(close: pd.DataFrame, market_index: pd.Series, *, omit_sndk: bool = False):
    """Compute exact V50/V100 causal inputs needed for monthly Ridge training."""
    eligible = [s for s in close.columns if s != 'SNDK' or not omit_sndk]
    ma28 = close.rolling(28, min_periods=28).mean()
    ready = ma28[eligible].notna() & close[eligible].gt(0) & np.isfinite(close[eligible])
    breadth = (close[eligible].gt(ma28[eligible]) & ready).sum(axis=1) / ready.sum(axis=1).replace(0, np.nan)
    index = market_index.reindex(close.index)
    daily = close.pct_change(fill_method=None)
    mm = index.pct_change(fill_method=None)
    market_var = mm.rolling(63).var().replace(0, np.nan)
    beta = daily.rolling(63).cov(mm).div(market_var, axis=0).clip(-1, 4)
    residual = daily - beta.mul(mm, axis=0)
    risk = daily.rolling(42).std().replace(0, np.nan)
    score = residual.rolling(42).sum().div(risk * np.sqrt(42))
    return eligible, index, breadth, residual, score, risk


def train_monthly_ridge_artifact(
    adjusted_close: pd.DataFrame,
    market_index: pd.Series,
    *,
    omit_sndk: bool = False,
    provenance: Mapping | None = None,
) -> dict:
    """Offline-only monthly causal StandardScaler + Ridge(alpha=80) trainer.

    Imports scikit-learn lazily inside this function so online runtime code never
    depends on or imports scikit-learn.
    """
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if adjusted_close.index.has_duplicates or not adjusted_close.index.is_monotonic_increasing:
        raise ValueError('unique monotonic session index required for offline Ridge training')

    eligible, market, breadth, residual, score, risk = _compute_base_series(
        adjusted_close, market_index, omit_sndk=omit_sndk
    )
    close = adjusted_close[eligible]
    res = residual[eligible]
    sc = score[eligible]
    rk = risk[eligible]

    # Exact label definition from research_v86_v100.policies.predict_residual (unclipped beta).
    forward = close.shift(-LABEL_MATURITY_SESSIONS).div(close).sub(1)
    benchmark_forward = market.shift(-LABEL_MATURITY_SESSIONS).div(market).sub(1)
    rr = close.pct_change(fill_method=None)
    mm = market.pct_change(fill_method=None)
    vv = mm.rolling(63).var().replace(0, np.nan)
    label_beta = rr.rolling(63).cov(mm).div(vv, axis=0)
    label = (forward - label_beta.mul(benchmark_forward, axis=0)).div(rk * np.sqrt(5))

    blocks = []
    market21 = market.pct_change(21, fill_method=None)
    for symbol in close.columns:
        trend = close[symbol].div(close[symbol].rolling(28).mean()).sub(1)
        blocks.append(pd.DataFrame({
            'ticker': symbol,
            'momentum': sc[symbol],
            'trend': trend,
            'relative5': res[symbol].rolling(5).sum(),
            'risk': rk[symbol],
            'breadth': breadth,
            'market21': market21,
            'label': label[symbol],
        }))
    features = list(FEATURE_NAMES)
    panel = pd.concat(blocks).sort_index().replace([np.inf, -np.inf], np.nan).dropna(subset=features)

    calendar = close.index
    months = calendar.to_period('M')
    models = {}
    insufficient_months = []

    for month in months.unique():
        month_key = str(month)
        days = calendar[months == month]
        i = calendar.get_loc(days[0])
        if i < LABEL_MATURITY_SESSIONS:
            insufficient_months.append(month_key)
            continue
        start_day = calendar[max(0, i - TRAIN_LOOKBACK_SESSIONS)]
        cutoff_day = calendar[i - LABEL_MATURITY_SESSIONS]
        learn = panel.loc[start_day:cutoff_day].dropna(subset=['label'])
        score_now = panel.loc[days[0]:days[-1]]
        if learn.index.nunique() < MIN_TRAIN_DAYS or score_now.empty:
            insufficient_months.append(month_key)
            continue
        model = make_pipeline(StandardScaler(), Ridge(alpha=RIDGE_ALPHA))
        model.fit(learn[features], learn.label.clip(-3, 3))
        scaler = model.named_steps['standardscaler']
        ridge = model.named_steps['ridge']
        models[month_key] = {
            'month': month_key,
            'effective_from_signal_date': str(days[0].date()),
            'label_maturity_date': str(days[0].date()),
            'train_start': str(start_day.date()),
            'train_cutoff': str(cutoff_day.date()),
            'train_days': int(learn.index.nunique()),
            'train_rows': int(len(learn)),
            'scaler_mean': [float(v) for v in scaler.mean_],
            'scaler_scale': [float(v) for v in scaler.scale_],
            'ridge_coef': [float(v) for v in ridge.coef_],
            'ridge_intercept': float(ridge.intercept_),
        }

    artifact = {
        'schema_version': SCHEMA_VERSION,
        'kind': ARTIFACT_KIND,
        'alpha': RIDGE_ALPHA,
        'min_train_days': MIN_TRAIN_DAYS,
        'train_lookback_sessions': TRAIN_LOOKBACK_SESSIONS,
        'label_maturity_sessions': LABEL_MATURITY_SESSIONS,
        'feature_names': list(FEATURE_NAMES),
        'eligible_symbols': list(eligible),
        'omit_sndk': bool(omit_sndk),
        'covered_start': str(calendar[0].date()) if len(calendar) else None,
        'covered_end': str(calendar[-1].date()) if len(calendar) else None,
        'covered_months': [str(m) for m in months.unique()],
        'insufficient_months': insufficient_months,
        'models': models,
        'provenance': dict(provenance or {}),
    }
    artifact['artifact_sha256'] = canonical_artifact_digest(artifact)
    return validate_ridge_artifact(artifact)


def validate_ridge_artifact(artifact: Mapping, *, expected_omit_sndk: bool | None = None) -> dict:
    """Validate frozen Ridge artifact schema, digest and strict causality of every month."""
    if not isinstance(artifact, Mapping):
        raise ModelArtifactError('ridge artifact must be a mapping')
    if artifact.get('schema_version') != SCHEMA_VERSION or artifact.get('kind') != ARTIFACT_KIND:
        raise ModelArtifactError('unsupported ridge artifact schema/kind')
    if tuple(artifact.get('feature_names', ())) != FEATURE_NAMES:
        raise ModelArtifactError('ridge artifact feature_names mismatch')
    if float(artifact.get('alpha', -1)) != RIDGE_ALPHA:
        raise ModelArtifactError('ridge artifact alpha mismatch')
    if expected_omit_sndk is not None and bool(artifact.get('omit_sndk')) != bool(expected_omit_sndk):
        raise ModelArtifactError('ridge artifact omit_sndk mismatch')
    if 'artifact_sha256' in artifact:
        if artifact['artifact_sha256'] != canonical_artifact_digest(artifact):
            raise ModelArtifactError('ridge artifact digest mismatch')

    models = artifact.get('models')
    if not isinstance(models, Mapping):
        raise ModelArtifactError('ridge artifact models map required')
    n_features = len(FEATURE_NAMES)
    for month_key, entry in models.items():
        if not isinstance(entry, Mapping) or entry.get('month') != month_key:
            raise ModelArtifactError(f'invalid month entry: {month_key}')
        eff = pd.Timestamp(entry['effective_from_signal_date'])
        cutoff = pd.Timestamp(entry['train_cutoff'])
        start = pd.Timestamp(entry['train_start'])
        maturity = pd.Timestamp(entry.get('label_maturity_date', entry['effective_from_signal_date']))
        if not (start <= cutoff < maturity <= eff):
            raise ModelArtifactError(
                f'noncausal training window in month {month_key}: '
                f'{start}..{cutoff} (maturity {maturity}) vs {eff}'
            )
        if int(entry['train_days']) < MIN_TRAIN_DAYS:
            raise ModelArtifactError(f'insufficient train_days in month {month_key}')
        for vector_key in ('scaler_mean', 'scaler_scale', 'ridge_coef'):
            vec = entry.get(vector_key)
            if not isinstance(vec, (list, tuple)) or len(vec) != n_features:
                raise ModelArtifactError(f'invalid {vector_key} length in month {month_key}')
            arr = np.asarray(vec, dtype=float)
            if not np.isfinite(arr).all():
                raise ModelArtifactError(f'non-finite {vector_key} in month {month_key}')
            if vector_key == 'scaler_scale' and (arr <= 0).any():
                raise ModelArtifactError(f'non-positive scaler_scale in month {month_key}')
        intercept = float(entry['ridge_intercept'])
        if not np.isfinite(intercept):
            raise ModelArtifactError(f'non-finite ridge_intercept in month {month_key}')
    return dict(artifact)


def select_causal_month_model(
    artifact: Mapping,
    signal_date: pd.Timestamp,
    *,
    strict_month_coverage: bool = True,
) -> tuple[str | None, dict | None]:
    """Select the causal monthly model for signal_date without any future leakage.

    - If month M is present in artifact['models'] and effective_from_signal_date <= signal_date,
      returns (M, entry).
    - If signal_date is within covered_end and M is in insufficient_months (warm-up period with
      <126 training days), returns (M, None) so forecast is NaN as in the original strategy.
    - If signal_date is strictly after covered_end (or month M is absent outside insufficient_months):
      raises ModelArtifactStaleError when strict_month_coverage=True.
    - NEVER selects a model whose effective_from_signal_date > signal_date or train_cutoff >= signal_date.
    """
    day = pd.Timestamp(signal_date).normalize()
    month_key = str(day.to_period('M'))
    models = artifact['models']
    if month_key in models:
        entry = models[month_key]
        eff = pd.Timestamp(entry['effective_from_signal_date']).normalize()
        cutoff = pd.Timestamp(entry['train_cutoff']).normalize()
        if cutoff >= day or eff > day:
            raise ModelArtifactError(
                f'causal violation for {day.date()}: model {month_key} has cutoff={cutoff.date()}, effective={eff.date()}'
            )
        return month_key, entry

    insufficient = set(artifact.get('insufficient_months', ()))
    covered_end = artifact.get('covered_end')
    if month_key in insufficient:
        return month_key, None
    if covered_end is not None and day <= pd.Timestamp(covered_end).normalize():
        # Historical date prior to first fitted month (e.g. shorter slice or warmup).
        return month_key, None

    if strict_month_coverage:
        raise ModelArtifactStaleError(
            f'no causal Ridge model for signal date {day.date()} (month {month_key}, artifact covered_end={covered_end})'
        )

    # Optional explicit causal carry-forward (never looks ahead).
    eligible_entries = [
        (m, e) for m, e in models.items()
        if pd.Timestamp(e['effective_from_signal_date']).normalize() <= day
        and pd.Timestamp(e['train_cutoff']).normalize() < day
    ]
    if not eligible_entries:
        return month_key, None
    eligible_entries.sort(key=lambda pair: pair[0])
    return eligible_entries[-1]


def _predict_matrix(values: np.ndarray, entry: Mapping) -> np.ndarray:
    mean = np.asarray(entry['scaler_mean'], dtype=float)
    scale = np.asarray(entry['scaler_scale'], dtype=float)
    coef = np.asarray(entry['ridge_coef'], dtype=float)
    intercept = float(entry['ridge_intercept'])
    standardized = (values - mean) / scale
    return standardized @ coef + intercept


def predict_residual_from_artifact(
    close: pd.DataFrame,
    residual: pd.DataFrame,
    score: pd.DataFrame,
    risk: pd.DataFrame,
    breadth: pd.Series,
    market: pd.Series,
    artifact: Mapping,
    *,
    strict_month_coverage: bool = False,
) -> pd.DataFrame:
    """Pure NumPy/Pandas causal inference across all dates in close.index.

    Produces predictions matching research_v86_v100.policies.predict_residual without
    importing scikit-learn or performing any model fitting.
    """
    validate_ridge_artifact(artifact)
    features = list(FEATURE_NAMES)
    panel = compute_v100_ridge_features(close, residual, score, risk, breadth, market)
    panel = panel.dropna(subset=features)
    calendar = close.index
    months = calendar.to_period('M')
    predictions = pd.DataFrame(np.nan, index=calendar, columns=close.columns)

    for month in months.unique():
        days = calendar[months == month]
        score_now = panel.loc[days[0]:days[-1]]
        if score_now.empty:
            continue
        first_predict_day = score_now.index.min()
        # Require and validate a causal monthly model only when the month has actual prediction rows.
        _, entry = select_causal_month_model(
            artifact,
            first_predict_day,
            strict_month_coverage=strict_month_coverage,
        )
        if entry is None:
            continue
        preds = _predict_matrix(score_now[features].to_numpy(dtype=float), entry)
        row = score_now[['ticker']].assign(score=preds)
        wide = row.set_index('ticker', append=True).score.unstack()
        predictions.loc[wide.index, wide.columns] = wide
    return predictions


def score_signal_date_from_artifact(
    close: pd.DataFrame,
    residual: pd.DataFrame,
    score: pd.DataFrame,
    risk: pd.DataFrame,
    breadth: pd.Series,
    market: pd.Series,
    signal_date: pd.Timestamp,
    artifact: Mapping,
    *,
    strict_month_coverage: bool = True,
) -> tuple[pd.Series, dict]:
    """Online single-date causal inference for one signal date (strictly no future rows)."""
    validate_ridge_artifact(artifact)
    day = pd.Timestamp(signal_date).normalize()
    if day not in close.index:
        raise ValueError(f'signal_date {day.date()} missing from close index')
    month_key, entry = select_causal_month_model(artifact, day, strict_month_coverage=strict_month_coverage)
    out = pd.Series(np.nan, index=close.columns, dtype=float)
    meta = {
        'signal_date': str(day.date()),
        'model_month': month_key,
        'train_cutoff': None if entry is None else entry['train_cutoff'],
        'effective_from_signal_date': None if entry is None else entry['effective_from_signal_date'],
        'features': {},
    }
    if entry is None:
        return out, meta

    market21_val = float(market.pct_change(21, fill_method=None).at[day])
    breadth_val = float(breadth.at[day])
    ma28_day = close.rolling(28).mean().loc[day]
    rel5_day = residual.rolling(5).sum().loc[day]
    for symbol in close.columns:
        c_val = float(close.at[day, symbol])
        m28 = float(ma28_day.at[symbol])
        trend_val = (c_val / m28 - 1.0) if (np.isfinite(c_val) and np.isfinite(m28) and m28 != 0.0) else np.nan
        vec = np.array([
            float(score.at[day, symbol]),
            trend_val,
            float(rel5_day.at[symbol]),
            float(risk.at[day, symbol]),
            breadth_val,
            market21_val,
        ], dtype=float)
        if np.isfinite(vec).all():
            out.at[symbol] = float(_predict_matrix(vec.reshape(1, -1), entry)[0])
            meta['features'][symbol] = {name: float(val) for name, val in zip(FEATURE_NAMES, vec)}
    return out, meta


def dump_artifact_python_module(artifact: Mapping, *, variable_name: str = 'MODEL_ARTIFACT') -> str:
    """Serialize a validated Ridge artifact into a deterministic pure-Python module for V2 packages."""
    validated = validate_ridge_artifact(artifact)
    body = json.dumps(validated, indent=2, sort_keys=True, allow_nan=False)
    return (
        '"""Frozen offline-trained monthly Ridge model artifact (auto-generated, read-only)."""\n'
        'import json\n\n'
        f'{variable_name} = json.loads({body!r})\n'
    )


def extract_frozen_artifact_from_module_source(source: str, *, variable_name: str = 'MODEL_ARTIFACT') -> dict:
    """Parse a frozen artifact Python module via AST only (zero execution) and validate it.

    Guarantees that an artifact-only package refresh cannot smuggle arbitrary Python code
    inside ``model_artifact.py``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ModelArtifactError('invalid syntax in model artifact module') from exc
    stmts = list(tree.body)
    if (
        stmts
        and isinstance(stmts[0], ast.Expr)
        and isinstance(stmts[0].value, ast.Constant)
        and isinstance(stmts[0].value.value, str)
    ):
        stmts = stmts[1:]
    if len(stmts) != 2:
        raise ModelArtifactError('model artifact module must contain only import json and artifact assignment')
    imp, assign = stmts
    if (
        not isinstance(imp, ast.Import)
        or len(imp.names) != 1
        or imp.names[0].name != 'json'
        or imp.names[0].asname is not None
    ):
        raise ModelArtifactError('model artifact module may only import json')
    if (
        not isinstance(assign, ast.Assign)
        or len(assign.targets) != 1
        or not isinstance(assign.targets[0], ast.Name)
        or assign.targets[0].id != variable_name
        or not isinstance(assign.value, ast.Call)
        or not isinstance(assign.value.func, ast.Attribute)
        or not isinstance(assign.value.func.value, ast.Name)
        or assign.value.func.value.id != 'json'
        or assign.value.func.attr != 'loads'
        or len(assign.value.args) != 1
        or assign.value.keywords
        or not isinstance(assign.value.args[0], ast.Constant)
        or not isinstance(assign.value.args[0].value, str)
    ):
        raise ModelArtifactError('model artifact module must assign MODEL_ARTIFACT = json.loads(<str>)')
    try:
        payload = json.loads(assign.value.args[0].value)
    except json.JSONDecodeError as exc:
        raise ModelArtifactError('invalid JSON payload in model artifact module') from exc
    return validate_ridge_artifact(payload)


def verify_ridge_artifact_compatibility(
    old_artifact: Mapping,
    new_artifact: Mapping,
    *,
    direction: str = 'upgrade',
    atol: float = 1e-9,
    rtol: float = 1e-9,
) -> None:
    """Verify two frozen Ridge artifacts are compatible for position-preserving refresh or rollback."""
    if direction not in {'upgrade', 'rollback'}:
        raise ModelArtifactError(f'unsupported compatibility direction: {direction!r}')
    old = validate_ridge_artifact(old_artifact)
    new = validate_ridge_artifact(new_artifact)
    for key in (
        'schema_version',
        'kind',
        'alpha',
        'min_train_days',
        'train_lookback_sessions',
        'label_maturity_sessions',
        'feature_names',
        'eligible_symbols',
        'omit_sndk',
    ):
        if old.get(key) != new.get(key):
            raise ModelArtifactError(f'incompatible Ridge artifact metadata: {key}')

    old_models = old['models']
    new_models = new['models']
    if direction == 'upgrade':
        if old.get('covered_end') and new.get('covered_end'):
            if pd.Timestamp(new['covered_end']) < pd.Timestamp(old['covered_end']):
                raise ModelArtifactError('model refresh cannot shrink covered_end on upgrade')
        missing_history = sorted(set(old_models) - set(new_models))
        if missing_history:
            raise ModelArtifactError(
                f'model refresh dropped previously fitted months: {", ".join(missing_history[:5])}'
            )

    common_months = sorted(set(old_models) & set(new_models))
    for month_key in common_months:
        om, nm = old_models[month_key], new_models[month_key]
        for field in ('train_start', 'train_cutoff', 'effective_from_signal_date', 'train_days', 'train_rows'):
            if om.get(field) != nm.get(field):
                raise ModelArtifactError(f'historical month {month_key} changed {field}')
        for vec_field in ('scaler_mean', 'scaler_scale', 'ridge_coef'):
            if not np.allclose(om[vec_field], nm[vec_field], rtol=rtol, atol=atol):
                raise ModelArtifactError(f'historical month {month_key} changed {vec_field}')
        if not np.isclose(om['ridge_intercept'], nm['ridge_intercept'], rtol=rtol, atol=atol):
            raise ModelArtifactError(f'historical month {month_key} changed ridge_intercept')
