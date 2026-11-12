"""Bounded real-history strategy replay with explicitly hypothetical paper liquidity."""
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.integrations.binance.store import Reader
from factorlab.forward.model_bundle import fit_final_model, save_model_bundle
from .contracts import Market
from .frozen_strategy import (FrozenFeature, FrozenDeploymentBinding, FixedScoreConversion,
                              FrozenModelStrategy, deployment_contract_sha256)
from .realtime import RealtimeAccount
from .simulation import PAPER_SIMULATION
from .strategy import ClosedBar

SOURCE = 'BINANCE_USDM_PUBLIC_REST'
FEATURES = (FrozenFeature('return_1', 'return', 1),
            FrozenFeature('mean_return_6', 'mean_return', 6),
            FrozenFeature('volatility_24', 'volatility', 24))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def load_snapshot_bars(path, *, max_bars=2160):
    """Return a pinned reader and a bounded contiguous prefix of confirmed bars."""
    if not 30 <= max_bars <= 2160:
        raise ValueError('max_bars must be in [30,2160]')
    reader = Reader(path)
    contract = reader.manifest['contract']
    if (contract['symbol'] != 'NVDAUSDT'
            or contract['instrument_id'] != 'BINANCE:USDM:NVDAUSDT'
            or contract['exchange_info']['symbol'] != 'NVDAUSDT'
            or reader.manifest['interval'] not in {'1h', '1d'}):
        raise ValueError('snapshot deployment identity mismatch')
    interval = {'1h': 3600, '1d': 86400}[reader.manifest['interval']]
    result = reader.read('bars')
    if result.metadata.source_id != SOURCE:
        raise ValueError('snapshot source mismatch')
    frame = result.data.iloc[:max_bars]
    bars = []
    for row in frame.itertuples(index=False):
        opened = pd.Timestamp(row.open_time).to_pydatetime()
        boundary = opened + timedelta(seconds=interval)
        if (pd.Timestamp(row.close_time) + pd.Timedelta(milliseconds=1) != pd.Timestamp(boundary)
                or int(opened.timestamp()) % interval != 0):
            raise ValueError('snapshot bar interval or close boundary mismatch')
        bar = ClosedBar('NVDAUSDT', opened, opened + timedelta(seconds=interval),
                        *(Decimal(str(getattr(row, name))) for name in
                          ('open', 'high', 'low', 'close', 'volume')), interval, SOURCE)
        if bars and bars[-1].closed_at != bar.opened_at:
            raise ValueError('snapshot bars must be contiguous')
        bars.append(bar)
    return reader, bars


def fit_frozen_ridge(reader, bars, output, git_commit):
    """Fit one fixed Ridge on chronological Train, never refit or select on Test."""
    if bars[0].interval_seconds != 3600:
        raise ValueError('fixed Ridge evidence requires hourly bars')
    warmup = max(f.required_bars for f in FEATURES) - 1
    rows = []
    for i in range(warmup, len(bars) - 2):
        rows.append(dict(bar_index=i, signal_time=bars[i].closed_at,
                         exit_time=bars[i + 2].opened_at,
                         target=float(bars[i + 2].open / bars[i + 1].open - 1),
                         **{f.column: f.value(bars[i + 1 - f.required_bars:i + 1]) for f in FEATURES}))
    frame = pd.DataFrame(rows)
    first, second = int(len(frame) * .6), int(len(frame) * .8)
    if first < 2 or second <= first:
        raise ValueError('insufficient rows for chronological split')
    validation_start = pd.Timestamp(frame.iloc[first].signal_time).ceil('D')
    test_start = pd.Timestamp(frame.iloc[second].signal_time).ceil('D')
    if test_start <= validation_start:
        test_start = validation_start + pd.Timedelta(days=1)
    train = frame.loc[(frame.signal_time < validation_start)
                      & (frame.exit_time < validation_start)].copy()
    validation = frame.loc[(frame.signal_time >= validation_start)
                           & (frame.signal_time < test_start)
                           & (frame.exit_time < test_start)].copy()
    test = frame.loc[frame.signal_time >= test_start].copy()
    if train.empty or validation.empty or test.empty:
        raise ValueError('insufficient history for day-aligned chronological Train/Validation/Test')
    # Bundle audit dates are calendar days; exact hourly eligibility is fixed above.
    train['date'] = train.signal_time.map(lambda x: x.date().isoformat())
    train['exit_date'] = train.exit_time.map(lambda x: x.date().isoformat())
    contract = dict(features=[asdict(f) for f in FEATURES], label='NEXT_OPEN_TO_FOLLOWING_OPEN_1H',
                    split='chronological_60_20_20_boundaries_ceiled_to_UTC_day',
                    purge='label_exit_strictly_before_next_signal',
                    validation_start=validation_start.isoformat(), test_start=test_start.isoformat(),
                    train_last_exit=train.exit_time.max().isoformat(),
                    params=dict(alpha=1.0, fit_intercept=True, max_iter=10000, tolerance=.0001),
                    seed=42, threshold=dict(long=.0001, exit=-.0001), target_weight='.05')
    source_hash = reader.manifest['data_version']
    contract_hash = deployment_contract_sha256(
        market='BINANCE_USDM', symbol='NVDAUSDT', interval_seconds=3600,
        source=SOURCE, features=FEATURES)
    fitted = fit_final_model(train, model_name='ridge', params=contract['params'],
                             feature_names=[f.column for f in FEATURES], target='target',
                             training_asof=train.exit_time.max().date().isoformat(), seed=42)
    bundle = save_model_bundle(fitted, Path(output) / 'bundles', model_version='fixed-ridge-v1',
                              source_dataset_sha256=reader.manifest['tables']['bars']['sha256'],
                              source_sha256=source_hash, universe_version=digest(['NVDAUSDT']),
                              research_contract_sha256=contract_hash, git_commit=git_commit)
    binding = FrozenDeploymentBinding('BINANCE_USDM', 'NVDAUSDT', 3600, SOURCE,
                                     source_hash, contract_hash, FEATURES)
    strategy = FrozenModelStrategy(bundle, strategy_id='fixed_ridge', strategy_version='1',
                                   expected_model_id='ridge', expected_model_version='fixed-ridge-v1',
                                   expected_bundle_id=bundle.metadata['content_sha256'], binding=binding,
                                   conversion=FixedScoreConversion(.0001, -.0001, Decimal('.05')))
    quality = {}
    predictions = []
    for name, part in [('train', train), ('validation', validation), ('test', test)]:
        predicted = bundle.predict(part[[f.column for f in FEATURES]]).to_numpy()
        actual = part.target.to_numpy()
        quality[name] = dict(rows=len(part), rmse=float(np.sqrt(np.mean((predicted - actual)**2))),
                             mae=float(np.mean(abs(predicted - actual))),
                             directional_accuracy=float(np.mean((predicted > 0) == (actual > 0))),
                             zero_return_rmse=float(np.sqrt(np.mean(actual**2))))
        predictions.extend(dict(split=name, signal_time=row.signal_time.isoformat(),
                                exit_time=row.exit_time.isoformat(), actual=float(row.target),
                                prediction=float(prediction))
                           for row, prediction in zip(part.itertuples(), predicted))
    output = Path(output)
    deployment = dict(bundle=str(bundle.path.resolve()), identity=dict(
        strategy_id='fixed_ridge', strategy_version='1', expected_model_id='ridge',
        expected_model_version='fixed-ridge-v1', expected_bundle_id=bundle.metadata['content_sha256']),
        binding=asdict(binding), conversion=dict(long_threshold=.0001, exit_threshold=-.0001,
                                               target_weight='.05'))
    (output / 'deployment.json').write_text(json.dumps(deployment, indent=2), encoding='utf-8')
    (output / 'research_contract.json').write_text(json.dumps(contract, indent=2))
    pd.DataFrame(predictions).to_csv(output / 'prediction_quality.csv', index=False)
    return strategy, dict(contract=contract, quality=quality, bundle=dict(bundle.metadata)), int(test.iloc[0].bar_index)


def replay_history(path, strategy, bars, public_symbol, *, start_index=0):
    """Replay closed bars and next-open paper events through normal account risk."""
    clock = [bars[0].closed_at]
    account = RealtimeAccount(path, Market.BINANCE_USDM, clock=lambda: clock[0],
                              strategy=strategy, simulation_policy=PAPER_SIMULATION)
    curve = []
    try:
        account.store.record_metadata('history-replay-assumptions', dict(
            kind='HISTORY_REPLAY_ASSUMPTIONS', prices=SOURCE,
            liquidity='HYPOTHETICAL_PAPER_LIQUIDITY_NOT_OBSERVED_TRADES',
            mark='HISTORICAL_OPEN_PROXY_NOT_OBSERVED_MARK',
            rules='SNAPSHOT_FILTERS_REFRESHED_OFFLINE_NOT_HISTORICAL_PIT',
            funding='NOT_SIMULATED', initial_cash='1000', target_weight='.05'))
        first_bar = max(0, start_index - strategy.history_limit + 1)
        for i in range(first_bar, len(bars) - 1):
            bar = bars[i]
            clock[0] = bar.closed_at
            if i == start_index:
                account.control('start')
            account.closed_bar(bar)
            if i < start_index:
                continue
            next_open = bars[i + 1].open
            for offset in (1, 2):
                clock[0] = bar.closed_at + timedelta(milliseconds=offset)
                common = dict(symbol='NVDAUSDT', time=clock[0].isoformat(),
                              received=clock[0].isoformat(), source=SOURCE)
                if offset == 1:
                    account.event(dict(common, kind='rules', payload=dict(symbol=public_symbol),
                                       reference=str(next_open)))
                    account.event(dict(common, kind='mark', price=str(next_open)))
                account.event(dict(common, kind='trade', price=str(next_open), size='100',
                                   liquidity_basis='HYPOTHETICAL_PAPER_LIQUIDITY_NOT_OBSERVED_TRADES'))
            if i >= start_index:
                view = account.controller.account()
                equity = view.assets[0].total + sum((p.quantity * (p.mark_price - p.entry_price)
                                                     for p in view.positions), Decimal(0))
                curve.append(dict(time=clock[0].isoformat(), equity=str(equity)))
        runtime = account.state.runtime
        return dict(signals=runtime['signals'], orders=runtime['orders'], fills=runtime['fills'],
                    rejected=runtime['rejected'], final_equity=curve[-1]['equity'],
                    paper_return=float(Decimal(curve[-1]['equity']) / 1000 - 1), equity_curve=curve,
                    account=asdict(account.controller.account()),
                    order_records=[asdict(order) for order in account.state.orders.values()],
                    fill_confirmations=[asdict(event) for event in account.state.confirmations.values()
                                        if event.cumulative_filled_quantity > 0],
                    total_fees=str(sum((order.snapshot.cumulative_fee
                                       for order in account.state.orders.values()), Decimal(0))),
                    last_rejection=runtime.get('last_rejection'),
                    liquidity='HYPOTHETICAL_PAPER_LIQUIDITY_NOT_OBSERVED_TRADES')
    finally:
        account.close()
