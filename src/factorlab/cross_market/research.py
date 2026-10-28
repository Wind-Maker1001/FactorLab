"""Shared factors, models, leakage-safe folds and market-specific execution."""
from dataclasses import asdict
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
from factorlab.models import FactorScoreModel, RidgeResearchModel, LightGBMResearchModel, XGBoostResearchModel
from factorlab.validation.splitter import ExpandingWalkForwardSplitter
from factorlab.data.csmar.native import peak_memory_bytes
from .contracts import ContractError, instant, session_date
from .factors import FACTOR_METADATA, compute_instrument_batch, preprocess
from .engine import target_weights, ExecutionEngine, fast_backtest
from .data import file_sha, implementation_sha
from . import WARNINGS
from .benchmark import validate_benchmark, attach_benchmark
from .adapters import ChinaAshareAdapter, USEquityAdapter
from .identity import DynamicUniverse, IdentityRegistry


def validate_adapter(adapter, spec):
    """Bind the specification to the supported adapter and its actual universe."""
    expected = {'CN': ChinaAshareAdapter, 'US': USEquityAdapter}[spec.market]
    if (type(adapter) is not expected or adapter.market != spec.market or
            adapter.currency != spec.currency or adapter.calendar.market != spec.market or
            adapter.rules.market != spec.market):
        raise ContractError('adapter/spec identity mismatch')
    universe = adapter.universe
    if (type(universe) is not DynamicUniverse or universe.market != spec.market or
            universe.name != spec.universe or type(universe.identities) is not IdentityRegistry):
        raise ContractError('adapter/spec universe identity mismatch')
    aliases = universe.identities.aliases
    if not aliases.loc[aliases.market.eq(spec.market), 'currency'].eq(spec.currency).all():
        raise ContractError('universe identity currency mismatch')
    if spec.universe in {'CSI500', 'US_INDEX'}:
        membership = universe.membership
        if (membership is None or membership.empty or
                {'date', 'instrument_id', 'available_at'} - set(membership)):
            raise ContractError('historical index membership required')
        member_dates = pd.DatetimeIndex([session_date(d) for d in membership.date])
        available = pd.DatetimeIndex([instant(t) for t in membership.available_at])
        # Recheck mutable inputs and require historically available membership
        # throughout the requested interval, including execution at the open.
        for day in adapter.calendar.sessions(spec.start_date, spec.end_date).index:
            rows = member_dates == day
            if not rows.any() or (available[rows] > adapter.calendar.at(day, 'open')).any():
                raise ContractError('historical index membership unavailable: ' + str(day.date()))
            for which in ('open', 'close'):
                if universe.members(day, adapter.calendar.at(day, which)).empty:
                    raise ContractError('historical index membership unavailable: ' + str(day.date()))
    elif universe.membership is not None:
        raise ContractError('all-market universe identity cannot contain index membership')


def execution_eligibility(pred, bars, adapter, spec, benchmark):
    """Keep OOS predictions intact; conservatively cap execution at the request.

    Extra supplied rows never implicitly authorize a longer research interval.
    Internal holes remain errors in the original price/action validators.
    """
    ledger = adapter.corporate_actions
    first = max(pd.Timestamp(spec.start_date), bars.date.min(), benchmark.date.min(),
                getattr(ledger, 'start', pd.Timestamp(spec.start_date)))
    last = min(pd.Timestamp(spec.end_date), bars.date.max(), benchmark.date.max(),
               getattr(ledger, 'end', pd.Timestamp(spec.end_date)))
    sessions = adapter.calendar.sessions(first, last).index if first <= last else pd.DatetimeIndex([])
    execution_dates = {}
    for day in pred.date.unique():
        try:
            execution_dates[day] = adapter.calendar.shift(day, 1)
        except ContractError:
            execution_dates[day] = pd.NaT
    audit = pred[['date', 'instrument_id']].copy()
    audit['execution_date'] = pd.to_datetime(audit.date.map(execution_dates))
    audit['executable'] = audit.date.isin(sessions) & audit.execution_date.isin(sessions)
    audit['reason'] = np.where(audit.executable, 'EXECUTABLE', 'NEXT_EXECUTION_OUTSIDE_VALIDATED_COVERAGE')
    return audit, sessions


def make_model(name, names, estimators=40):
    if name == 'FactorScore':
        return FactorScoreModel({n: (-1.0 if n.startswith(('REV', 'VOL', 'AMIHU')) else 1.0) for n in names})
    if name == 'Ridge':
        return RidgeResearchModel(feature_names=names)
    if name == 'LightGBM':
        return LightGBMResearchModel(feature_names=names, n_estimators=estimators, n_jobs=1)
    if name == 'XGBoost':
        return XGBoostResearchModel(feature_names=names, n_estimators=estimators, n_jobs=1)
    raise ContractError('unknown model')


def build_dataset(bars, adapter, spec, instrument_batch=256, retrospective=False):
    names = list(spec.factor_names or FACTOR_METADATA)
    ids = sorted(bars.instrument_id.unique())
    pieces = []
    for offset in range(0, len(ids), instrument_batch):
        group = bars.loc[bars.instrument_id.isin(ids[offset:offset + instrument_batch])]
        frame = compute_instrument_batch(group, adapter.calendar, spec.market, names, spec.holding_sessions, strict_vintage=not retrospective)
        pieces.append(frame)
    f = pd.concat(pieces, ignore_index=True)
    selected = []
    for day, cross in f.loc[f.observed & f.halted.eq(False)].groupby('date'):
        as_of = pd.Timestamp('2026-10-27T00:00:00Z') if retrospective else adapter.calendar.at(day, 'close')
        eligible = adapter.universe.members(day, as_of).instrument_id
        cross = cross.loc[cross.instrument_id.isin(eligible)]
        cross = adapter.rules.filter_research_observations(cross)
        selected.append(cross)
    f = pd.concat(selected, ignore_index=True)
    f = f.loc[f.date.between(spec.start_date, spec.end_date)]
    f = preprocess(f, names, minimum_count=max(spec.top_k))
    # Warmups/missing required observations explicitly unavailable. No neutral
    # factor substitution even though individual legacy models support imputation.
    f['factors_available'] = np.isfinite(f[names]).all(axis=1)
    return f


def run_models(dataset, spec, calendar, output, estimators=40, retain_predictions=True):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    names = list(spec.factor_names or FACTOR_METADATA)
    # Prediction eligibility uses historical features, never the existence of
    # future returns. A missing delisting/exit price cannot remove an OOS name.
    complete = dataset.loc[dataset.factors_available].sort_values(['date', 'instrument_id']).reset_index(drop=True)
    complete['symbol'] = complete.instrument_id  # opaque compatibility grouping key
    splitter = ExpandingWalkForwardSplitter(initial_train_months=spec.train_months,
        validation_months=spec.validation_months, test_months=spec.test_months, step_months=spec.test_months,
        purge_days=spec.purge_sessions, embargo_days=spec.embargo_sessions)
    training_calendar = calendar.schedule.index
    training_calendar = training_calendar[(training_calendar >= complete.date.min()) & (training_calendar <= complete.date.max())]
    folds = list(splitter.split(complete, calendar=training_calendar))
    if not folds:
        raise ContractError('insufficient complete history for configured train/validation/test windows')
    outputs, receipts = {}, []
    for name in spec.model:
        prediction_parts = []
        model_started = time.perf_counter()
        for fold in folds:
            train, validation, test = (complete.iloc[idx] for idx in fold.as_indices())
            train = train.loc[np.isfinite(train.target)]
            validation = validation.loc[np.isfinite(validation.target)]
            if train.empty or validation.empty:
                raise ContractError('no observed training/validation labels in fold')
            model = make_model(name, names, estimators)
            model.fit(train[names], train.target)
            # Validation is evaluated; no test data selects parameters.
            validation_score = model.predict(validation[names])
            score = model.predict(test[names])
            evaluable = np.isfinite(test.target)
            pred = test[['date', 'instrument_id', 'target', 'signal_at']].copy()
            pred['score'] = score.to_numpy()
            pred['fold'] = fold.fold
            prediction_parts.append(pred)
            receipts.append(dict(model=name, fold=fold.fold, train_rows=len(train), validation_rows=len(validation), test_rows=len(test),
                train_first=str(train.date.min().date()), train_last=str(train.date.max().date()),
                test_first=str(test.date.min().date()), test_last=str(test.date.max().date()),
                train_max_exit=str(train.exit_date.max()), validation_max_exit=str(validation.exit_date.max()),
                validation_first=str(validation.date.min()), test_first_signal=str(test.date.min()),
                validation_mse=float(np.mean((validation_score.to_numpy() - validation.target.to_numpy()) ** 2)),
                test_mse=float(np.mean((score.to_numpy()[evaluable] - test.target.to_numpy()[evaluable]) ** 2)) if evaluable.any() else None,
                test_labeled_rows=int(evaluable.sum()), test_unlabeled_rows=int((~evaluable).sum()),
                purged_dates=len(fold.purged_dates), embargoed_dates=len(fold.embargoed_dates)))
        predictions = pd.concat(prediction_parts, ignore_index=True)
        if predictions.duplicated(['date', 'instrument_id']).any():
            raise ContractError('overlapping OOS predictions')
        predictions.to_parquet(output / (name + '-predictions.parquet'), index=False)
        outputs[name] = predictions if retain_predictions else str(output / (name + '-predictions.parquet'))
        print(json.dumps({'model': name, 'folds': len(folds), 'oos_rows': len(predictions), 'seconds': round(time.perf_counter() - model_started, 2)}), flush=True)
    (output / 'folds.json').write_text(json.dumps(receipts, indent=2), encoding='utf-8')
    return outputs, receipts


def run_research(bars, adapter, spec, output, *, benchmark, synthetic=False, retrospective=False, instrument_batch=256, estimators=40):
    started = time.perf_counter()
    validate_adapter(adapter, spec)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    benchmark = validate_benchmark(benchmark, adapter.calendar, spec.start_date, spec.end_date, spec.currency)
    if set(benchmark.benchmark_id) != {spec.benchmark}:
        raise ContractError('benchmark identity differs from research configuration')
    benchmark.to_parquet(output / 'benchmark.parquet', index=False)
    dataset = build_dataset(bars, adapter, spec, instrument_batch, retrospective)
    dataset.to_parquet(output / 'dataset.parquet', index=False)
    predictions, folds = run_models(dataset, spec, adapter.calendar, output / 'models', estimators)
    backtests = []
    execution_signals = []
    for name, pred in predictions.items():
        audit, covered_sessions = execution_eligibility(pred, bars, adapter, spec, benchmark)
        audit.to_parquet(output / (name + '-execution-eligibility.parquet'), index=False)
        execution_signals.append(dict(model=name, oos_rows=len(pred), executable_rows=int(audit.executable.sum()),
            excluded_rows=int((~audit.executable).sum()), reasons=audit.reason.value_counts().to_dict(),
            coverage_first=str(covered_sessions.min()), coverage_last=str(covered_sessions.max())))
        for k in spec.top_k:
            targets = target_weights(pred, adapter.calendar, k, spec.rebalance_frequency)
            scheduled_rows = len(targets)
            targets = targets.loc[targets.date.isin(audit.loc[audit.executable, 'date'])].copy()
            targets.to_parquet(output / (name + '-top' + str(k) + '-targets.parquet'), index=False)
            first = targets.date.min()
            last = min(covered_sessions.max(), audit.execution_date.max())
            for mode in ('fast', 'execution'):
                record = dict(model=name, top_k=k, mode=mode, scheduled_signal_rows=scheduled_rows,
                    execution_signal_rows=len(targets), excluded_signal_rows=scheduled_rows-len(targets))
                try:
                    if targets.empty or pd.isna(last) or first >= last:
                        raise ContractError('no executable signals or valid valuation interval within validated coverage')
                    sessions = adapter.calendar.sessions(first, last).index
                    ids = sorted(set(bars.instrument_id) | set(targets.instrument_id))
                    adapter.corporate_actions.require_coverage(first, last, ids)
                    if hasattr(adapter.corporate_actions, 'require_session'):
                        for day in sessions:
                            adapter.corporate_actions.require_session(day, ids, adapter.calendar.at(day, 'open'))
                    result = fast_backtest(bars, targets, adapter.calendar, adapter.fees, first, last) if mode == 'fast' else ExecutionEngine(adapter).run(bars, targets, first, last)
                    benchmark_slice = benchmark.loc[benchmark.date.isin(sessions)]
                    if (not pd.DatetimeIndex(result.nav.date).equals(sessions) or
                            not pd.DatetimeIndex(benchmark_slice.date).equals(sessions)):
                        raise ContractError('Fast/Execution/benchmark valuation dates differ')
                    result.nav = attach_benchmark(result.nav, benchmark)
                    prefix = name + '-top' + str(k) + '-' + mode
                    for attr in ('nav', 'trades', 'positions', 'rejections', 'actions'):
                        getattr(result, attr).to_parquet(output / (prefix + '-' + attr + '.parquet'), index=False)
                    record.update(status='PASS', days=len(result.nav), trades=len(result.trades),
                                  terminal_nav=float(result.nav.nav.iloc[-1]), return_definition=result.return_definition)
                except ContractError as exc:
                    record.update(status='BLOCKED', reason=str(exc))
                backtests.append(record)
    status = ('PASS' if backtests and all(r['status'] == 'PASS' for r in backtests) else
              'PARTIAL' if any(r['status'] == 'PASS' for r in backtests) else 'BLOCKED')
    receipt = dict(status=status,
        classification='SYNTHETIC / SOFTWARE_ONLY' if synthetic else 'REAL_SOURCE / RETROSPECTIVE_UNVERIFIED / EXPLORATORY_ONLY',
        spec=asdict(spec), implementation_sha256=implementation_sha(), input_rows=len(bars),
        instrument_count=int(bars.instrument_id.nunique()), dataset_rows=len(dataset),
        unavailable_factor_rows=int((~dataset.factors_available).sum()), models=list(predictions), fold_count=len(folds) // len(spec.model),
        actual_start=str(bars.date.min().date()), actual_end=str(bars.date.max().date()),
        backtests=backtests, execution_signals=execution_signals,
        adapter_identity=dict(adapter_class=type(adapter).__name__, market=adapter.market,
            currency=adapter.currency, universe_class=type(adapter.universe).__name__, universe=adapter.universe.name),
        seconds=time.perf_counter() - started, peak_memory_bytes=peak_memory_bytes(),
        gates=list(WARNINGS), formal_admission='BLOCKED', execution_admission='BLOCKED',
        real_us_data='US_REAL_DATA_NOT_VALIDATED', artifacts={p.relative_to(output).as_posix(): file_sha(p) for p in output.rglob('*') if p.is_file()})
    (output / 'run.json').write_text(json.dumps(receipt, indent=2, default=str), encoding='utf-8')
    return receipt
