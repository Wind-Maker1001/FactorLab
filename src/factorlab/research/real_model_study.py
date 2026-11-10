"""Retrospective raw-price prediction study; no execution or HFQ admission."""
from itertools import combinations

import numpy as np
import pandas as pd

from factorlab.dataset.identity import frame_hash, json_hash
from factorlab.exploratory.factor_discovery.contracts import FIELDS, Node
from factorlab.exploratory.factor_discovery.engine import compile_expression
from factorlab.factors.registry_v2 import FactorDefinition, FactorRegistrySnapshot, FactorDataContext
from factorlab.validation.factor_selection import FactorSelectionConfig, TrainFactorSelector
from factorlab.validation.splitter import ExpandingWalkForwardSplitter
from factorlab.validation.walk_forward import WalkForwardRunner

KEYS = ['date', 'symbol']
PRICES = ['raw_open', 'raw_high', 'raw_low', 'raw_close']
TARGET = 'RAW_FWD_OPEN5'


def raw_registry():
    close = Node('Field', value='raw_close')
    volume = Node('Field', value='volume')
    def ratio(left, right):
        return Node('SafeDiv', (left, right))
    def mean(node, window):
        return Node('RollingMean', (node,), param=window)
    expressions = {
        **{f'RAW_MOM{w}': Node('Sub', (ratio(close, Node('Lag', (close,), param=w)), Node('Const', value=1.0))) for w in (5, 20, 60)},
        'RAW_TREND20': Node('Sub', (ratio(close, mean(close, 20)), Node('Const', value=1.0))),
        'RAW_VOL20': Node('RollingStd', (Node('Sub', (ratio(close, Node('Lag', (close,), param=1)), Node('Const', value=1.0))),), param=20),
        'RAW_RANGE20': mean(ratio(Node('Sub', (Node('Field', value='raw_high'), Node('Field', value='raw_low'))), close), 20),
        'RAW_VOLUME5_20': ratio(mean(volume, 5), mean(volume, 20)),
    }
    definitions = tuple(FactorDefinition.from_expression(name, 'raw-exploratory-v1', compile_expression(node)) for name, node in expressions.items())
    snapshot = FactorRegistrySnapshot(definitions)
    return snapshot, tuple(d.ref for d in definitions)


def require_raw(config):
    if config['price_basis'] != 'RAW_PRICE_EXPLORATORY':
        raise ValueError('HFQ_UNVERIFIED: only explicitly named RAW_PRICE_EXPLORATORY is permitted')


def check_source_identity(actual, expected):
    pins = {(r['archive'], r['member']): r['actual_sha256'] for r in expected if 'archive' in r}
    for row in actual:
        if pins.get((row['archive'], row['member'])) != row['actual_sha256']:
            raise ValueError('data identity drift: ' + row['archive'] + '/' + row['member'])
        if row.get('hash_matches_receipt') is False:
            raise ValueError('vendor receipt identity drift')


def build_panel(bars, members, calendar, config):
    """Normalize on signal-known eligibility, never future-label availability."""
    require_raw(config)
    dates = pd.DatetimeIndex(calendar)
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('calendar must be unique and increasing')
    if bars.duplicated(KEYS).any() or members.duplicated(KEYS).any():
        raise ValueError('duplicate security-date identity')
    symbols = sorted(bars.symbol.unique())
    grid = pd.MultiIndex.from_product([symbols, dates], names=['symbol', 'date'])
    panel = bars.set_index(['symbol', 'date']).reindex(grid).reset_index()
    values = panel[PRICES + ['volume']].to_numpy(dtype=float)
    ready = (np.isfinite(values).all(axis=1) & panel[PRICES].gt(0).all(axis=1)
             & panel.volume.gt(0) & panel.suspended.eq(0) & panel.st_status.eq(0)
             & panel.raw_low.le(panel[['raw_open', 'raw_close']].min(axis=1))
             & panel.raw_high.ge(panel[['raw_open', 'raw_close']].max(axis=1))
             & panel.raw_low.le(panel.raw_high))
    missing_keys = int(panel.raw_open.isna().sum())
    invalid_ohlc = int((panel.raw_low.gt(panel[['raw_open', 'raw_close']].min(axis=1)) | panel.raw_high.lt(panel[['raw_open', 'raw_close']].max(axis=1))).sum())
    panel.loc[~ready, PRICES + ['volume']] = np.nan
    # Interpreter requires its full whitelist; unavailable, unused fields stay NaN.
    for name in FIELDS:
        if name != 'RET1' and name not in panel:
            panel[name] = np.nan
    snapshot, refs = raw_registry()
    features = snapshot.compute(panel, refs, calendar=dates, context=FactorDataContext(data_source='REAL_CANDIDATE_BIGQUANT_RAW'))
    names = [ref.factor_id for ref in refs]
    history_ready = ready.groupby(panel.symbol, sort=False).transform(lambda v: v.rolling(61, min_periods=61).sum().eq(61))
    eligible = members.merge(panel[KEYS].assign(signal_ready=history_ready.to_numpy()), on=KEYS, how='left', validate='one_to_one')
    eligible = eligible.merge(features, on=KEYS, how='left', validate='one_to_one')
    signal = eligible.signal_ready.eq(True) & eligible[names].notna().all(axis=1)
    scaled = eligible.loc[signal, KEYS + names].copy()
    grouped = scaled.groupby('date')[names]
    mu, sigma = grouped.transform('mean'), grouped.transform(lambda v: v.std(ddof=0))
    scaled[names] = (scaled[names] - mu) / sigma.replace(0, np.nan)
    # Endpoints t+1 and t+6 are five open-to-open intervals. Every intervening
    # observed session must have valid raw fields and non-suspended status.
    grouped_panel = panel.groupby('symbol', sort=False)
    entry = grouped_panel.raw_open.shift(-1)
    exit_price = grouped_panel.raw_open.shift(-6)
    path = pd.Series(ready.to_numpy(), index=panel.index)
    valid_path = path.copy()
    for offset in range(1, 7):
        valid_path &= path.groupby(panel.symbol, sort=False).shift(-offset).eq(True)
    labels = panel[KEYS].copy()
    labels[TARGET] = (exit_price / entry - 1).where(valid_path)
    jump = grouped_panel.raw_close.pct_change(fill_method=None).abs().gt(0.2)
    jump_path = jump.copy()
    for offset in range(1, 7):
        jump_path |= jump.groupby(panel.symbol, sort=False).shift(-offset).eq(True)
    labels['possible_action_jump_path'] = jump_path
    position = dates.get_indexer(panel.date)
    for name, offset in [('entry_date', 1), ('exit_date', 6)]:
        labels[name] = pd.to_datetime([dates[p + offset] if p + offset < len(dates) else pd.NaT for p in position])
    combined = scaled.merge(labels, on=KEYS, validate='one_to_one')
    complete = combined[names + [TARGET]].notna().all(axis=1)
    data = combined.loc[complete].sort_values(KEYS).reset_index(drop=True)
    counts = data.groupby('date').symbol.transform('size')
    data = data.loc[counts.ge(config['minimum_cross_section'])].reset_index(drop=True)
    profile = dict(member_days=len(members), signal_feature_ready=int(signal.sum()),
        complete_feature_label_rows=int(complete.sum()), common_rows=len(data), common_dates=int(data.date.nunique()),
        missing_raw_open_grid_rows=missing_keys, rejected_grid_rows=int((~ready).sum()),
        common_possible_action_jump_paths=int(data.possible_action_jump_path.sum()),
        suspended_source_rows=int(panel.suspended.eq(1).sum()),
        unknown_status_grid_rows=int((~panel.suspended.isin([0, 1]) | panel.st_status.isna()).sum()),
        nonfinite_ohlcv_grid_rows=int((~np.isfinite(values).all(axis=1)).sum()),
        invalid_ohlc_source_rows=invalid_ohlc,
        feature_missing_rate={n: float(eligible[n].isna().mean()) for n in names},
        feature_coverage=float(signal.mean()), label_evaluable_given_features=float(complete.mean()),
        label_evaluable_member_rate=float(eligible[KEYS].merge(labels, on=KEYS, how='left')[TARGET].notna().mean()))
    return data, profile, snapshot, refs


def make_runner(config, model, names, selector=None):
    options = dict(model_name=model, feature_names=names, target_col=TARGET,
        random_seed=config['seed'], minimum_cross_section=config['minimum_cross_section'],
        top_k=config['top_k'], factor_selector=selector)
    p = config['parameters']
    if model == 'factor_score':
        options.update(normalize_by_absolute_weight=True)
        if selector is None:
            options['factor_weights'] = dict.fromkeys(names, 1.0)
        else:
            options['dynamic_factor_score_weighting'] = 'equal'
    elif model == 'ridge':
        options.update(ridge_alphas=(p[model]['alpha'],), ridge_fit_intercept=p[model]['fit_intercept'],
            ridge_max_iter=p[model]['max_iter'], ridge_tolerance=p[model]['tolerance'])
    else:
        options[model + '_candidates'] = (p[model],)
        options[model + '_n_jobs'] = config['threads']
    return WalkForwardRunner(ExpandingWalkForwardSplitter(**config['splitter']), **options)


def common_predictions(parts):
    baseline = None
    for key, part in parts.items():
        local = part.sort_values(['fold', *KEYS]).reset_index(drop=True)
        if not np.isfinite(local.score).all():
            raise ValueError('nonfinite model prediction: ' + key)
        identity = frame_hash(local[['fold', *KEYS, TARGET]])
        if baseline is not None and baseline != identity:
            raise ValueError('models must use identical test keys and labels')
        baseline = identity
    return baseline


def daily_metrics(predictions, top_k, minimum_cross_section):
    rows = []
    for (model, fold, day), part in predictions.groupby(['model_id', 'fold', 'signal_date'], sort=True):
        n = len(part)
        ic = part.prediction.corr(part[TARGET], method='spearman') if n >= minimum_cross_section else np.nan
        order = part.sort_values(['prediction', 'symbol'], kind='stable')
        k = min(top_k, n // 2)
        spread = order[TARGET].iloc[-k:].mean() - order[TARGET].iloc[:k].mean() if k else np.nan
        rows.append(dict(model_id=model, fold=fold, date=day, year=pd.Timestamp(day).year, rank_ic=ic, top_bottom_raw_price_change=spread, observations=n))
    return pd.DataFrame(rows)


def hac_mean_se(values, lag=20):
    """Bartlett HAC standard error of the equal-date mean (no IID row test)."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return None
    centered = x - x.mean()
    variance = np.dot(centered, centered) / len(x)
    for k in range(1, min(lag, len(x) - 1) + 1):
        variance += 2 * (1 - k / (lag + 1)) * np.dot(centered[k:], centered[:-k]) / len(x)
    return float(np.sqrt(max(variance, 0) / len(x)))


def summarize_daily(daily, groups):
    rows = []
    for keys, part in daily.groupby(groups, sort=True):
        keys = keys if isinstance(keys, tuple) else (keys,)
        valid = part.rank_ic.dropna()
        sd = valid.std(ddof=1)
        rows.append(dict(zip(groups, keys), rank_ic=float(valid.mean()),
            icir=float(valid.mean() / sd) if sd > 0 else None,
            positive_date_ratio=float(valid.gt(0).mean()), effective_cross_sections=len(valid),
            security_days=int(part.observations.sum()), missing_rank_ic_dates=int(part.rank_ic.isna().sum()),
            top_bottom_raw_price_change=float(part.top_bottom_raw_price_change.mean()), hac20_se=hac_mean_se(valid)))
    return pd.DataFrame(rows)


def paired_daily(daily):
    rows = []
    wide = daily.pivot(index=['fold', 'date'], columns='model_id', values='rank_ic')
    for left, right in combinations(wide.columns, 2):
        delta = wide[left] - wide[right]
        valid = delta.dropna()
        rows.append(dict(left=left, right=right, paired_dates=len(valid),
            mean_rank_ic_difference=float(valid.mean()), hac20_se=hac_mean_se(valid),
            positive_difference_ratio=float(valid.gt(0).mean())))
    return pd.DataFrame(rows)
