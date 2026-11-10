"""Publish saved P9-01 evidence without fitting or executing another account."""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.dataset.identity import file_hash, json_hash


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def publish(source, output):
    source, output = Path(source), Path(output)
    config = json.loads((source / 'config.json').read_text(encoding='utf-8'))
    identity = json.loads((source / 'inference-identity.json').read_text(encoding='utf-8'))
    reconstruction = json.loads((source / 'reconstruction.json').read_text(encoding='utf-8'))
    result = json.loads((source / 'result.json').read_text(encoding='utf-8'))
    if json_hash(config) != identity['config_hash']:
        raise ValueError('execution configuration identity drift')
    if file_hash(source / 'candidate-predictions.parquet') != identity['candidate_prediction_sha256']:
        raise ValueError('candidate prediction identity drift')
    expected = {(m, f) for m in config['models'] for f in range(3)}
    if {(r['model_id'], r['fold']) for r in reconstruction} != expected or not all(r['matched'] for r in reconstruction):
        raise ValueError('incomplete model reconstruction evidence')
    predictions = pd.read_parquet(source / 'candidate-predictions.parquet')
    output.mkdir(parents=True, exist_ok=False)
    dump(output / 'reconstruction.json', reconstruction)
    counts = predictions.groupby(['model_id', 'fold']).size().rename('signal_candidate_rows').reset_index()
    counts.to_csv(output / 'inference-counts.csv', index=False)
    metrics_rows, gap_parts, daily_parts, prefix_rows = [], [], [], []
    for fold in range(3):
        for model in config['models'] + config['benchmarks']:
            name = model+'-fold-'+str(fold)
            path = source / name
            metrics_file = path / 'metrics.json'
            metrics = json.loads(metrics_file.read_text(encoding='utf-8')) if metrics_file.exists() else {}
            status = metrics.get('status', 'NOT_RUN')
            reason = metrics.get('reason', 'STOPPED_AFTER_DATA_BOUNDARY')
            if name == result.get('interrupted_account'):
                status, reason = 'BLOCKED', result['reason']
            if model == 'cash':
                status, reason = 'REFERENCE_ONLY', 'MODEL_ZERO_INTEREST_CASH_NOT_EXECUTED_STOCK_ACCOUNT'
                metrics = dict(cumulative_return=0., annualized_return=0., annualized_volatility=0., maximum_drawdown=0.)
            metrics_rows.append(dict(model_id=model, fold=fold, status=status, reason=reason,
                cumulative_return=metrics.get('cumulative_return'), annualized_return=metrics.get('annualized_return'),
                annualized_volatility=metrics.get('annualized_volatility'), sharpe_ratio=metrics.get('sharpe_ratio'),
                maximum_drawdown=metrics.get('maximum_drawdown'), calmar_ratio=metrics.get('calmar_ratio'),
                total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None))
            if (path / 'valuation-gaps.csv').exists():
                gaps = pd.read_csv(path / 'valuation-gaps.csv', parse_dates=['date'])
                if len(gaps):
                    gap_parts.append(gaps[['date', 'symbol']].assign(model_id=model, fold=fold))
                    # Only publish the causally unaffected, observed-close prefix.
                    # It is not a substitute Test fold or a profitability selection.
                    end = gaps.date.min()
                    nav = pd.read_parquet(path / 'nav.parquet')
                    nav = nav.loc[nav.date.lt(end)]
                    trades = pd.read_parquet(path / 'trades.parquet')
                    trades = trades.loc[trades.execution_date.lt(end)]
                    rejections = pd.read_parquet(path / 'rejections.parquet')
                    rejections = rejections.loc[rejections.execution_date.lt(end)]
                    deviations = pd.read_parquet(path / 'target_comparison.parquet')
                    deviations = deviations.loc[deviations.execution_date.lt(end)]
                    costs = {k:float(trades[k].sum()) for k in ('commission', 'stamp_duty', 'slippage', 'transaction_cost')}
                    prefix_rows.append(dict(model_id=model, fold=fold, scope='OBSERVED_PREFIX_NOT_FULL_TEST',
                        start=str(identity['folds'][fold]['test'][0]), end=str(nav.date.max().date()),
                        cumulative_net_price_return=float(nav.strategy_nav.iloc[-1]-1),
                        same_fill_gross_price_return=float(nav.strategy_nav.iloc[-1]-1+costs['transaction_cost']/config['initial_cash']),
                        average_exposure=float((1-nav.cash/nav.portfolio_value).mean()),
                        average_holdings=float(nav.holdings.mean()), turnover=float(nav.turnover.sum()),
                        trades=len(trades), rejection_records=len(rejections),
                        rejection_reasons=rejections.reason.value_counts().to_dict(),
                        minimum_commission_increment=float((trades.commission-trades.trade_notional*config['fees']['commission_bps']/10000).clip(lower=0).sum()),
                        target_positive_mean_absolute_deviation=float(deviations.loc[deviations.target_weight.gt(0), 'weight_deviation'].abs().mean()),
                        fees=costs))
                    daily_parts.append(nav[['date', 'strategy_nav']].assign(model_id=model, fold=fold, scope='OBSERVED_PREFIX_NOT_FULL_TEST'))
            elif status == 'COMPLETED':
                nav = pd.read_csv(path / 'daily-nav.csv')
                daily_parts.append(nav[['date', 'strategy_nav']].assign(model_id=model, fold=fold, scope='FULL_TEST'))
    if result['status'] == 'P9_01_REAL_OOS_EXECUTION_COMPLETED' and any(
            row['status'] != 'COMPLETED' for row in metrics_rows if row['model_id'] != 'cash'):
        raise ValueError('completed status requires all predeclared stock accounts')
    pilot = json.loads((source / 'pilot.json').read_text(encoding='utf-8'))
    dump(output / 'pilot-metrics.json', pilot)
    pilot_nav = pd.read_csv(source / 'pilot/daily-nav.csv')
    daily_parts.append(pilot_nav[['date', 'strategy_nav']].assign(model_id=config['models'][0], fold=0, scope='PILOT_ONLY'))
    pd.concat(daily_parts, ignore_index=True).to_csv(output / 'daily-nav.csv', index=False)
    pd.DataFrame(metrics_rows).to_csv(output / 'fold-metrics.csv', index=False)
    if gap_parts:
        pd.concat(gap_parts, ignore_index=True).to_csv(output / 'valuation-gaps.csv', index=False)
    dump(output / 'observed-prefix-metrics.json', prefix_rows)
    private_hashes = {p.relative_to(source).as_posix():file_hash(p) for p in source.rglob('*') if p.is_file() and 'tracker' not in p.parts}
    summary = dict(status=result['status'], reason=result.get('reason'),
        interrupted_account=result.get('interrupted_account'),
        identity={k:identity[k] for k in ('kind', 'original_prediction_sha256', 'common_test_hash',
            'original_sample_hash', 'data_identity', 'feature_identity', 'candidate_hash', 'candidate_rows',
            'candidate_prediction_sha256', 'config_hash', 'training_config_hash', 'environment', 'code')},
        original_counts=dict(folds=3, dates=726, security_days=48998, prediction_rows=440982),
        inference_counts=counts.to_dict('records'), private_artifact_sha256=private_hashes,
        total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None, safety=config['safety'])
    dump(output / 'result.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    publish(args.source, args.output)
