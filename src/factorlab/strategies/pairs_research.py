"""Non-executable spread evidence using the existing train-only pair machinery."""
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from factorlab.strategies.pairs_statistics import PairModel, select_pairs


@dataclass
class StatisticalResult:
    selections: pd.DataFrame
    folds: pd.DataFrame
    diagnostics: pd.DataFrame
    signals: pd.DataFrame
    episodes: pd.DataFrame
    validation: pd.DataFrame


def _diagnostic_model(row):
    if pd.isna(row.get("coint_pvalue")):
        return None
    return PairModel(**{key: row[key] for key in PairModel.__dataclass_fields__})


def _signals(closes, dates, model, strategy, accepted):
    spread = model.spread(closes.loc[:dates[-1]])
    z = model.zscore(closes.loc[:dates[-1]], strategy.z_window)
    frame = pd.DataFrame(dict(date=dates, spread=spread.reindex(dates), zscore=z.reindex(dates)))
    frame["accepted"] = accepted
    frame["signal_kind"] = "ELIGIBLE_STATISTICAL_SIGNAL" if accepted else "REJECTED_CONTROL_DIAGNOSTIC_ONLY"
    frame["extreme"] = frame.zscore.abs().ge(strategy.entry_z)
    next_dates = pd.Series(closes.index, index=closes.index).shift(-1)
    frame["next_observed_open_date"] = next_dates.reindex(dates).to_numpy()
    # This is a spread observation, not a market order or a claim of execution.
    return frame.reset_index(drop=True)


def _episodes(signals, threshold, strategy):
    rows = []
    i = 0
    while i < len(signals) - 1:
        value = signals.zscore.iloc[i]
        if not np.isfinite(value) or abs(value) < threshold:
            i += 1
            continue
        stop = min(i + strategy.max_holding_sessions, len(signals) - 1)
        recovered, missing = False, False
        j = i
        for j in range(i + 1, stop + 1):
            observed = signals.zscore.iloc[j]
            if not np.isfinite(observed):
                missing = True
                break
            if abs(observed) <= strategy.exit_z or observed * value <= 0:
                recovered = True
                break
        censored = missing or (not recovered and stop - i < strategy.max_holding_sessions)
        rows.append(dict(signal_date=signals.date.iloc[i], observation_end=signals.date.iloc[j],
                         next_observed_open_date=signals.next_observed_open_date.iloc[i],
                         entry_z=threshold, initial_z=value, recovered=recovered,
                         elapsed_sessions=j-i, censored=censored,
                         reason="missing_observation" if missing else "recovered" if recovered else
                                "fold_boundary" if censored else "horizon_exhausted",
                         accepted=bool(signals.accepted.iloc[i])))
        i = j + 1
    return rows


def statistical_research(closes, *, selection, strategy, splitter, entry_grid):
    """Freeze training fits; validation describes, never optimizes, thresholds.

    Rejected pairs receive diagnostics only. Recovery is defined in causal
    rolling-Z space, not cash profit; missing windows and fold boundaries censor
    observations. Each threshold's episodes are nonoverlapping within a fold.
    """
    from statsmodels.tsa.stattools import adfuller

    if closes.shape[1] != 2:
        raise ValueError("statistical research requires one explicitly configured pair")
    if splitter.step_months < splitter.test_months:
        raise ValueError("overlapping OOS folds are not supported")
    grid = sorted(set(entry_grid))
    if not grid:
        raise ValueError("entry_grid must not be empty")
    for threshold in grid:
        replace(strategy, entry_z=threshold)
    calendar = closes.index
    horizon = strategy.max_holding_sessions + 2
    labels = pd.DataFrame(dict(date=calendar, entry_date=pd.Series(calendar).shift(-1),
                               exit_date=pd.Series(calendar).shift(-horizon)))
    selections, folds, diagnostics, signals, episodes, validation = [], [], [], [], [], []
    for fold in splitter.split(labels, calendar=calendar):
        cfg = replace(selection, train_start=fold.train_dates[0].isoformat(), train_end=fold.train_dates[-1].isoformat())
        models, table = select_pairs(closes, cfg)
        row = table.iloc[0].to_dict()
        selections.append(dict(fold=fold.fold, **row))
        accepted = bool(models)
        model = models[0] if accepted else _diagnostic_model(row)
        folds.append(dict(fold=fold.fold, train_start=cfg.train_start, train_end=cfg.train_end,
                          validation_start=fold.validation_dates[0], validation_end=fold.validation_dates[-1],
                          test_start=fold.test_dates[0], test_end=fold.test_dates[-1],
                          train_max_exit_date=fold.audit.train_max_exit_date,
                          validation_max_exit_date=fold.audit.validation_max_exit_date,
                          purged_sessions=len(fold.purged_dates), embargoed_sessions=len(fold.embargoed_dates),
                          train_overlap_rows_removed=fold.audit.train_overlap_rows_removed,
                          validation_overlap_rows_removed=fold.audit.validation_overlap_rows_removed,
                          entry_z=strategy.entry_z, accepted=accepted,
                          model=None if model is None else asdict(model)))
        for phase, dates in (("validation", fold.validation_dates), ("test", fold.test_dates)):
            # Short-window diagnostics do not change the training admission rule.
            dcfg = replace(selection, train_start=dates[0].isoformat(), train_end=dates[-1].isoformat(),
                           min_observations=20)
            _, diagnostic = select_pairs(closes, dcfg)
            drow = diagnostic.iloc[0].to_dict()
            drow.update(fold=fold.fold, phase=phase, diagnostic_min_observations=20,
                        frozen_residual_adf_pvalue=None, hedge_drift_from_training=None)
            if model is not None:
                residual = model.spread(closes.reindex(dates)).dropna()
                if len(residual) >= 20 and np.ptp(residual) > 0:
                    drow["frozen_residual_adf_pvalue"] = float(adfuller(residual, regression="n", autolag="AIC")[1])
                if model.hedge_ratio and pd.notna(drow.get("hedge_ratio")):
                    drow["hedge_drift_from_training"] = abs(drow["hedge_ratio"] - model.hedge_ratio) / abs(model.hedge_ratio)
                scored = _signals(closes, dates, model, strategy, accepted)
                scored["fold"] = fold.fold
                for threshold in grid:
                    events = _episodes(scored, threshold, strategy)
                    for event in events:
                        event.update(fold=fold.fold, phase=phase)
                    if phase == "test":
                        episodes.extend(events)
                    else:
                        validation.extend(events)
                if phase == "test":
                    signals.append(scored)
            diagnostics.append(drow)
    if not folds:
        raise ValueError("calendar does not contain a complete walk-forward fold")
    return StatisticalResult(pd.DataFrame(selections), pd.DataFrame(folds), pd.DataFrame(diagnostics),
                             pd.concat(signals, ignore_index=True) if signals else pd.DataFrame(),
                             pd.DataFrame(episodes), pd.DataFrame(validation))
