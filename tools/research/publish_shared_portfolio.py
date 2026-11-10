"""Publish portfolio-level derived evidence without private execution ledgers."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import pandas as pd
from factorlab.dataset.identity import file_hash

NAV_COLUMNS = ['date', 'strategy_nav', 'strategy_return', 'exposure', 'holdings',
               'concentration_hhi', 'maximum_security_weight']
SUMMARY_FIELDS = ['task', 'status', 'safety', 'annual_counts', 'annual_accounts', 'return_basis',
                  'total_return_status', 'total_return', 'market_index_status', 'execution_reference',
                  'manifest_sha256', 'bars_sha256', 'config_sha256', 'frozen_sample_sha256',
                  'code', 'implementation_sha256', 'environment', 'years', 'artifact_sha256']


def publish(source, output):
    source, output = Path(source), Path(output)
    summary = json.loads((source / 'summary.json').read_text(encoding='utf-8'))
    # Verify the saved receipts before publishing derived results, without executing again.
    for relative, expected in summary['artifact_sha256'].items():
        if file_hash(source / relative) != expected:
            raise ValueError('private artifact identity drift: ' + relative)
    public = {k: summary[k] for k in SUMMARY_FIELDS}
    public['private_summary_sha256'] = file_hash(source / 'summary.json')
    public['publisher_sha256'] = file_hash(Path(__file__))
    daily, rows, execution = [], [], []
    for year, account in summary['years'].items():
        if account['status'] != 'COMPLETED':
            continue
        for scheme, data in account['schemes'].items():
            metrics = data['metrics']
            row = dict(year=int(year), scheme=scheme, **{k: v for k, v in metrics.items() if k not in ('fees', 'rejection_reasons')})
            row.update({'fee_' + k: v for k, v in metrics.get('fees', {}).items()})
            rows.append(row)
            if scheme == 'cash':
                nav = pd.read_csv(source / year / 'cash-reference.csv')
                nav['exposure'], nav['concentration_hhi'], nav['maximum_security_weight'] = 0., 0., 0.
            else:
                folder = source / year / scheme
                nav = pd.read_csv(folder / 'nav.csv')
                trades = pd.read_csv(folder / 'trades.csv')
                rejects = pd.read_csv(folder / 'rejections.csv')
                comparison = pd.read_csv(folder / 'target_comparison.csv')
                positive = comparison.loc[comparison.target_weight.gt(0)]
                decisions = pd.read_csv(folder / 'decisions.csv')
                execution.append(dict(year=int(year), scheme=scheme,
                    snapshots=int(comparison.signal_date.nunique()),
                    rejected_shares=float(rejects.rejected_shares.sum()),
                    rejection_reasons=rejects.reason.value_counts().to_dict(),
                    buy_notional=float(trades.loc[trades.side.eq('buy'), 'trade_notional'].sum()),
                    sell_notional=float(trades.loc[trades.side.eq('sell'), 'trade_notional'].sum()),
                    target_positive_mean_absolute_deviation=float(positive.weight_deviation.abs().mean()),
                    target_positive_maximum_absolute_deviation=float(positive.weight_deviation.abs().max()),
                    decision_reasons=decisions.reason.value_counts().to_dict(),
                    all_execution_dates_after_signal=bool((pd.to_datetime(trades.execution_date) > pd.to_datetime(trades.signal_date)).all()),
                    all_main_board_fills_in_100_share_lots=bool(trades.executed_shares.mod(100).eq(0).all())))
            daily.append(nav[NAV_COLUMNS].assign(year=int(year), scheme=scheme))
    public['execution_summary'] = execution
    output.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(rows).to_csv(output / 'annual-metrics.csv', index=False)
    if daily:
        pd.concat(daily, ignore_index=True).to_csv(output / 'daily-nav.csv', index=False)
    (output / 'real-summary.json').write_text(json.dumps(public, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return public


def main():
    parser = argparse.ArgumentParser(description='P8-01 derived-only publication')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = publish(args.input, args.output)
    print(json.dumps(dict(status=result['status'], execution_summary=result['execution_summary']), ensure_ascii=False))


if __name__ == '__main__':
    main()
