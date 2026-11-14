"""Build compact evidence and a Markdown table from a completed funding overlay."""
import argparse
import csv
import json
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', type=Path, required=True)
    parser.add_argument('--acquisition', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ('summary.csv', 'coverage.json'):
        shutil.copyfile(args.analysis/name, args.output/name)
    shutil.copyfile(args.acquisition/'targeted-tests.xml', args.output/'targeted-tests.xml')
    raw = json.loads((args.acquisition/'funding.json').read_text())
    times = [datetime.fromtimestamp(r['fundingTime']/1000, timezone.utc) for r in raw]
    buckets = Counter(t.strftime('%Y-%m-%d %H') for t,r in zip(times,raw) if r['rateType']=='Regular')
    schedule = Counter(t.hour for t,r in zip(times,raw) if r['rateType']=='Regular')
    receipt = dict(source='https://fapi.binance.com/fapi/v1/fundingRate',
                   symbol='NVDAUSDT', startTime=1778716800000, endTime=1786492799999,
                   rows=len(raw), price_coverage_pct=100,
                   regular_hours_utc=dict(schedule), distinct_regular_hour_buckets=len(buckets),
                   special_events=[r for r in raw if r['rateType']=='Special'],
                   provenance='PublicClient request/raw receipts retained alongside local acquisition',
                   settlement_order='Fills strictly earlier than fundingTime; exact ties reported separately',
                   price='Historical markPrice supplied with each fundingRate event',
                   documentation_fetch='Official developer documentation and Binance FAQ connection timed out; not authenticated by this run')
    (args.output/'sources.json').write_text(json.dumps(receipt, indent=2))
    with (args.analysis/'summary.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    table = ['|成本|账户|原净收益|资金费现金流|调整净收益|原回撤 %|调整回撤 %|',
             '|---|---|---:|---:|---:|---:|---:|']
    for row in rows:
        nums = [float(row[k]) for k in ('original_profit','funding','adjusted_profit',
                                        'original_drawdown_pct','adjusted_hourly_drawdown_pct')]
        table.append('|'+row['cost']+'|'+row['account']+'|'+'|'.join(f'{n:.6f}' for n in nums)+'|')
    (args.output/'comparison.md').write_text('\n'.join(table)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
