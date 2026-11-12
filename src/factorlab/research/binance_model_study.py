"""Fixed NVDAUSDT historical holdout research, never deployment performance."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sqlite3
from collections import Counter

import numpy as np
import pandas as pd

from factorlab.forward.model_bundle import fit_final_model
from factorlab.trading.strategy import DualMAStrategy, StrategySignal
from factorlab.trading.strategy_replay import FEATURES, SOURCE, digest, load_snapshot_bars, replay_history

PARAMS = {
    'ridge': {'alpha': 1.0, 'fit_intercept': True, 'max_iter': 10000, 'tolerance': .0001},
    'lightgbm': {'n_estimators': 100, 'max_depth': 3, 'num_leaves': 7, 'n_jobs': 1},
    'xgboost': {'n_estimators': 100, 'max_depth': 3, 'n_jobs': 1},
}
COLUMNS = [f.column for f in FEATURES]


def dataset(bars):
    rows = []
    for i in range(max(f.required_bars for f in FEATURES) - 1, len(bars) - 2):
        rows.append(dict(bar_index=i, signal_time=bars[i].closed_at,
                         exit_time=bars[i + 2].opened_at,
                         target=float(bars[i + 2].open / bars[i + 1].open - 1),
                         **{f.column: f.value(bars[i + 1-f.required_bars:i+1]) for f in FEATURES}))
    frame = pd.DataFrame(rows)
    frame['date'] = frame.signal_time.map(lambda t: t.date().isoformat())
    frame['exit_date'] = frame.exit_time.map(lambda t: t.date().isoformat())
    return frame


def partitions(frame):
    validation = pd.Timestamp(frame.iloc[int(len(frame)*.6)].signal_time).ceil('D')
    test = pd.Timestamp(frame.iloc[int(len(frame)*.8)].signal_time).ceil('D')
    return {
        'train': frame[(frame.signal_time < validation) & (frame.exit_time < validation)],
        'validation': frame[(frame.signal_time >= validation) & (frame.exit_time < test)],
        'test': frame[frame.signal_time >= test],
    }


def quality(actual, predicted):
    actual, predicted = np.asarray(actual), np.asarray(predicted)
    zero_mse = float(np.mean(actual**2))
    mse = float(np.mean((predicted-actual)**2))
    return dict(rows=len(actual), rmse=float(np.sqrt(mse)), mae=float(np.mean(abs(predicted-actual))),
                directional_accuracy=float(np.mean(np.sign(predicted) == np.sign(actual))),
                zero_rmse=float(np.sqrt(zero_mse)), mse_skill=1-mse/zero_mse if zero_mse else None,
                correlation=float(np.corrcoef(actual, predicted)[0, 1])
                if np.std(actual) > 0 and np.std(predicted) > 0 else None,
                score_quantiles=dict(zip(['min','p01','p25','median','p75','p99','max'],
                    np.quantile(predicted, [0,.01,.25,.5,.75,.99,1]).tolist())),
                long_fraction=float(np.mean(predicted > .0001)),
                exit_fraction=float(np.mean(predicted < -.0001)))


def fit(name, train):
    return fit_final_model(train, model_name=name, params=PARAMS[name], feature_names=COLUMNS,
                           target='target', training_asof=train.exit_time.max().date().isoformat(), seed=42)


@dataclass
class ScoreStrategy:
    strategy_id: str
    model: object = None
    history_limit: int = 25

    def describe(self):
        return dict(strategy_id=self.strategy_id, strategy_version='fixed-v1', symbol='NVDAUSDT',
                    interval_seconds=3600, source=SOURCE, history_limit=self.history_limit, threshold=.0001,
                    weight='.05', features=COLUMNS, params=PARAMS.get(self.strategy_id))

    def signal(self, bars, now):
        bar = bars[-1]
        direction, target, status, score = 'HOLD', None, 'INSUFFICIENT_DATA', None
        if len(bars) >= self.history_limit:
            values = {f.column: f.value(bars[-f.required_bars:]) for f in FEATURES}
            score = (float(self.model.predict(pd.DataFrame([values], columns=COLUMNS)).iloc[0])
                     if self.model is not None else values['return_1'])
            if self.strategy_id == 'no_trade':
                score = 0.0
            status = 'NO_SIGNAL'
            if score > .0001:
                direction, target, status = 'BUY', Decimal('.05'), 'SIGNAL'
            elif score < -.0001:
                direction, target, status = 'SELL', Decimal(0), 'SIGNAL'
        return StrategySignal(self.strategy_id, 'fixed-v1', bar.symbol, now, bar.closed_at,
                              direction, target, SOURCE, digest([self.strategy_id, now.isoformat()]),
                              status=status, score=score)


def risk_summary(replay):
    equity = np.array([1000.] + [float(r['equity']) for r in replay['equity_curve']])
    returns = np.diff(equity)/equity[:-1]
    return dict(max_drawdown=float(np.min(equity/np.maximum.accumulate(equity)-1)),
                hourly_volatility=float(np.std(returns, ddof=1)),
                annualized_sharpe_descriptive=float(np.mean(returns)/np.std(returns, ddof=1)*np.sqrt(24*365))
                if np.std(returns, ddof=1) > 0 else None,
                sharpe_status='SHORT_SAMPLE_DESCRIPTIVE_NOT_SIGNIFICANCE',
                turnover_definition='sum(abs(fill_quantity * fill_price)) / initial_equity')


def ledger_summary(path, replay):
    from factorlab.trading.storage import decode
    with sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True) as db:
        events = [decode(json.loads(row[0])) for row in db.execute('SELECT payload FROM events')]
    rejections = Counter(e['reason'] for e in events if isinstance(e, dict) and e.get('kind') == 'TARGET_REJECTED')
    fills = replay['fill_confirmations']
    orders = {r['snapshot']['intent']['client_order_id']: r['snapshot']['intent'] for r in replay['order_records']}
    previous, quantity, notional, cursor, weights = {}, Decimal(0), Decimal(0), 0, []
    for point in replay['equity_curve']:
        while cursor < len(fills) and pd.Timestamp(fills[cursor]['event_time']) <= pd.Timestamp(point['time']):
            event = fills[cursor]
            key = event['client_order_id']
            cumulative = Decimal(event['cumulative_filled_quantity'])
            delta = cumulative - previous.get(key, Decimal(0))
            previous[key] = cumulative
            quantity += delta * (1 if orders[key]['side'] == 'BUY' else -1)
            notional += delta * Decimal(event['fill_price'])
            cursor += 1
        weights.append(quantity != 0)
    return dict(rejection_reasons=dict(rejections), traded_notional=float(notional),
                turnover_initial_equity=float(notional/Decimal(1000)), time_in_market=float(np.mean(weights)),
                final_positions=replay['account']['positions'],
                fill_time_first=fills[0]['event_time'] if fills else None)


def run(hourly, output):
    output.mkdir(parents=True, exist_ok=False)
    reader, bars = load_snapshot_bars(hourly)
    frame = dataset(bars)
    parts = partitions(frame)
    started = datetime.now(timezone.utc).isoformat()
    evidence = dict(source=SOURCE, data_version=reader.manifest['data_version'], bars=len(bars),
        history_start=bars[0].opened_at.isoformat(), history_end=bars[-1].closed_at.isoformat(),
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        actual_research_started_at=started, parameters=PARAMS, seed=42,
        safeguards=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'],
        interpretation='HISTORICAL_REPLAY_NOT_DEPLOYED_FORWARD_SIGNALS; PRIOR_RIDGE_TEST_ALREADY_OBSERVED',
        factor_score=dict(status='NOT_APPLICABLE', reason='Requires daily cross-sectional z-scores; one symbol'),
        walk_forward=dict(status='RESEARCH_TIME_SERIES_ADAPTER',
            reason='Existing runner requires >=2 cross-sectional members and calendar-day labels', folds=[]),
        costs=dict(fee=.001, slippage=.0001, spread='NOT_VERIFIED', liquidity='NOT_VERIFIED',
                   historical_fee='NOT_VERIFIED', funding='NOT_VERIFIED_NOT_APPLIED',
                   funding_note='P6-D02 has 275 events; account settlement applicability is not verified'),
        splits={k:dict(rows=len(v), first_signal=v.signal_time.min().isoformat(),
                       last_signal=v.signal_time.max().isoformat(), last_exit=v.exit_time.max().isoformat())
                for k,v in parts.items()}, quality={}, paper={})
    # Two pre-Test expanding folds, each assessed on a disjoint half of Validation.
    validation = parts['validation']
    middle = pd.Timestamp(validation.iloc[len(validation)//2].signal_time).ceil('D')
    for begin, end in [(validation.signal_time.min(), middle), (middle, parts['test'].signal_time.min())]:
        train = frame[(frame.signal_time < begin) & (frame.exit_time < begin)]
        held = frame[(frame.signal_time >= begin) & (frame.exit_time < end)]
        fold = dict(train_rows=len(train), evaluation_rows=len(held),
                    train_last_exit=train.exit_time.max().isoformat(),
                    evaluation_start=begin.isoformat(), evaluation_end=end.isoformat(), metrics={})
        for name in PARAMS:
            fitted = fit(name, train)
            fold['metrics'][name] = quality(held.target, fitted.model.predict(held[COLUMNS]))
        evidence['walk_forward']['folds'].append(fold)
    prediction_rows = []
    strategies = {}
    for name in [*PARAMS, 'zero', 'momentum']:
        fitted = fit(name, parts['train']) if name in PARAMS else None
        evidence['quality'][name] = {}
        if fitted:
            strategies[name] = ScoreStrategy(name, fitted.model)
        for split, part in parts.items():
            predicted = (fitted.model.predict(part[COLUMNS]).to_numpy() if fitted else
                         np.zeros(len(part)) if name == 'zero' else part.return_1.to_numpy())
            evidence['quality'][name][split] = quality(part.target, predicted)
            for row, score in zip(part.itertuples(), predicted):
                prediction_rows.append(dict(model=name, split=split, signal_time=row.signal_time,
                    entry_time=row.signal_time, exit_time=row.exit_time, actual=row.target, prediction=score))
            if split == 'test':
                evidence['quality'][name]['test_halves'] = [quality(a, p) for a,p in zip(
                    np.array_split(part.target.to_numpy(),2), np.array_split(predicted,2))]
    pd.DataFrame(prediction_rows).to_csv(output/'predictions.csv', index=False)
    strategies.update(momentum=ScoreStrategy('momentum'), no_trade=ScoreStrategy('no_trade'),
                      dual_ma=DualMAStrategy(max_weight=Decimal('.05')))
    for name, strategy in strategies.items():
        result = replay_history(output/f'{name}.sqlite', strategy, bars,
                                reader.manifest['contract']['exchange_info'],
                                start_index=int(parts['test'].iloc[0].bar_index))
        result.update(risk_summary(result))
        result.update(ledger_summary(output/f'{name}.sqlite', result))
        evidence['paper'][name] = result
    evidence['actual_research_finished_at'] = datetime.now(timezone.utc).isoformat()
    evidence['conclusions'] = ['P7_R01_RESEARCH_REPRODUCIBLE', 'P7_R01_EVIDENCE_LIMITED',
                               'INSUFFICIENT_HISTORY']
    if all(evidence['quality'][name]['test']['mse_skill'] <= 0
           and evidence['paper'][name]['paper_return'] <= 0 for name in PARAMS):
        evidence['conclusions'].append('P7_R01_NEGATIVE_RESULT')
    for result in evidence['paper'].values():
        result['net_return_minus_no_trade'] = result['paper_return']
        result['net_return_minus_dual_ma'] = result['paper_return'] - evidence['paper']['dual_ma']['paper_return']
        result['fee_addback_return_not_cost_free_counterfactual'] = result['paper_return'] + float(result['total_fees'])/1000
    (output/'evidence.json').write_text(json.dumps(evidence, indent=2, default=str, allow_nan=False), encoding='utf-8')
    print(output/'evidence.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hourly-snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.hourly_snapshot, args.output)


if __name__ == '__main__':
    main()
