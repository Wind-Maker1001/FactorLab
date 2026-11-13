"""Export compact lifecycle evidence without Paper databases or market copies."""
import argparse
import json
from pathlib import Path
import shutil

import pandas as pd

from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_paper import _write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--tests', required=True)
    parser.add_argument('--reproduction', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--r02-summary', default='docs/validation/p7-r02/summary.csv')
    args = parser.parse_args()
    study, output = Path(args.study), Path(args.output)
    source = pd.read_csv(study/'summary.csv')
    rows, fills, observations, accounts = [], [], [], []
    for row in source.itertuples():
        directory = study/row.scenario/row.arm/row.mode
        key = dict(scenario=row.scenario, arm=row.arm, mode=row.mode)
        rows.append(key | dict(wall_seconds=row.wall_seconds) | lifecycle_metrics(directory))
        for target, filename in ((fills, 'trades.csv'), (observations, 'daily.csv')):
            frame = pd.read_csv(directory/filename)
            for field, value in key.items():
                frame[field] = value
            target.append(frame)
        result = json.loads((directory/'result.json').read_text(encoding='utf-8'))
        accounts.append(key | {name: result[name] for name in ('config', 'rules', 'orders', 'risk', 'lifecycle')})
    summary = pd.DataFrame(rows)
    old = pd.read_csv(args.r02_summary)
    checks = []
    for mode, old_arm in (('arithmetic', 'arithmetic'), ('geometric', 'geometric'), ('atr', 'atr')):
        before = old.loc[(old.window=='full') & (old.scenario=='base') & (old.arm==old_arm)].iloc[0]
        after = summary.loc[(summary.scenario=='base') & (summary.arm=='A') & (summary['mode']==mode)].iloc[0]
        fields = ('final_equity', 'fills', 'buy_fills', 'sell_fills', 'fees', 'end_inventory')
        if any(abs(float(before[f])-float(after[f])) > 1e-9 for f in fields):
            raise ValueError(f'R02 baseline regression: {mode}')
        checks.append(dict(mode=mode, fields=list(fields), matches=True))
    output.mkdir(parents=True, exist_ok=False)
    summary.to_csv(output/'summary.csv', index=False)
    pd.concat(fills, ignore_index=True).to_csv(output/'fills.csv', index=False)
    pd.concat(observations, ignore_index=True).to_parquet(output/'hourly.parquet', index=False)
    _write(output/'accounts.json', accounts)
    _write(output/'r02-baseline.json', checks)
    for name in ('design.json', 'data-identity.json', 'study.json'):
        shutil.copyfile(study/name, output/name)
    shutil.copyfile(args.tests, output/'targeted-tests.xml')
    shutil.copyfile(Path(args.reproduction)/'reproduction.json', output/'reproduction.json')
    print(f'exported {len(summary)} reconciled accounts to {output}', flush=True)


if __name__ == '__main__':
    main()
