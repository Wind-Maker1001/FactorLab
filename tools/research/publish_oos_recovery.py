"""Publish saved P9-02 portfolio derivatives, never source data or trade ledgers."""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.dataset.identity import file_hash


def publish(source, output):
    output.mkdir(parents=True, exist_ok=False)
    for name in ('result.json', 'inference-verification.json', 'config.json', 'fold-metrics.json', 'code-identity.json'):
        value = json.loads((source / name).read_bytes())
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    stage = json.loads((source / 'stage-a/result.json').read_bytes())
    (output / 'stage-a.json').write_text(json.dumps(stage, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    stage_marks = source / 'stage-a/002408.SZ-valuations.parquet'
    if stage_marks.exists():
        marks = pd.read_parquet(stage_marks)
        marks.loc[marks.valuation_assumption_used].to_csv(output / 'stage-a-valuation-assumptions.csv', index=False)
    rejection = pd.read_parquet(source / 'stage-a/000540.SZ-rejections.parquet')
    fills = pd.read_parquet(source / 'stage-a/000540.SZ-trades.parquet')
    cash = pd.read_parquet(source / 'stage-a/000540.SZ-nav.parquet').set_index('date').cash
    diagnostic = dict(date='2023-05-08', symbol='000540.SZ',
        reason=rejection.loc[rejection.execution_date.eq('2023-05-08'), 'reason'].tolist(),
        fills_on_rejected_session=int(fills.execution_date.eq('2023-05-08').sum()),
        cash_before=float(cash.loc['2023-05-05']), cash_after=float(cash.loc['2023-05-08']),
        scope='STAGE_A_ISOLATED_REPRODUCTION_NOT_COMPLETE_FOLD',
        valuation_status='BLOCKED_BY_UNQUALIFIED_2023_05_04_HELD_CLOSE',
        fees={field:float(fills[field].sum()) for field in ('commission', 'stamp_duty', 'slippage', 'transaction_cost')})
    (output / 'stage-a-rejection.json').write_text(json.dumps(diagnostic, indent=2)+'\n', encoding='utf-8')
    gaps = pd.read_csv(source / 'stage-a/000540.SZ-valuation-gaps.csv')
    gaps[['date', 'symbol', 'raw_close']].to_csv(output / 'valuation-gaps.csv', index=False)
    if (source / 'daily-nav.csv').exists():
        pd.read_csv(source / 'daily-nav.csv').to_csv(output / 'daily-nav.csv', index=False)
    path = source / 'valuation-qualifications.parquet'
    if path.exists():
        qualifications = pd.read_parquet(path)
        qualifications.loc[qualifications.valuation_assumption_used].to_csv(output / 'valuation-assumptions.csv', index=False)
        daily = qualifications.groupby(['model_id', 'fold', 'date']).agg(
            held_security_count=('symbol','size'), model_valuation_security_count=('valuation_assumption_used','sum'))
        daily.to_csv(output / 'valuation-coverage.csv')
    rows = json.loads((source / 'fold-metrics.json').read_bytes())
    config = json.loads((source / 'config.json').read_bytes())['execution']
    recorded = {(row['model_id'], row['fold']):row['metrics'] for row in rows}
    statuses = [dict(model_id=scheme, fold=fold,
        status=recorded.get((scheme, fold), {}).get('status', 'NOT_RUN'),
        reason=recorded.get((scheme, fold), {}).get('reason',
            'STAGE_GATE_NOT_PASSED' if stage['status'] != 'PASSED' else 'NOT_RECORDED'))
        for fold in range(3) for scheme in config['models'] + config['benchmarks']]
    pd.DataFrame(statuses).to_csv(output / 'account-status.csv', index=False)
    flat = []
    for row in rows:
        metrics = row['metrics']
        flat.append(dict(model_id=row['model_id'], fold=row['fold'], **{
            k:v for k,v in metrics.items() if not isinstance(v, (dict, list))}))
    table = pd.DataFrame(flat) if flat else pd.DataFrame(columns=['model_id', 'fold', 'status',
        'net_return', 'annualized_return', 'annualized_volatility', 'sharpe', 'max_drawdown', 'calmar'])
    table.to_csv(output / 'fold-metrics.csv', index=False)
    receipt = dict(source_result_sha256=file_hash(source/'result.json'),
        files={p.name:file_hash(p) for p in output.iterdir() if p.is_file()})
    (output / 'publication.json').write_text(json.dumps(receipt, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    publish(args.source, args.output)
