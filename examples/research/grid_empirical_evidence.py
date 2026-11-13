"""Publish compact NAV and transaction evidence from a completed hourly study."""
import argparse
import json
from pathlib import Path
import shutil

import pandas as pd

from factorlab.experiments.grid_paper import _write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tests', type=Path, required=True)
    args = parser.parse_args()
    summary = pd.read_csv(args.study/'summary.csv')
    if len(summary) != 30 or not summary.ledger_reconciled.all():
        raise ValueError('complete reconciled 30-run study required')
    args.output.mkdir(parents=True, exist_ok=False)
    for name in ('summary.csv', 'data-identity.json', 'study.json'):
        shutil.copyfile(args.study/name, args.output/name)
    shutil.copyfile(args.tests, args.output/'targeted-tests.xml')
    navs, fills, orders, configs, risks = [], [], [], [], []
    for run in summary.itertuples():
        tags = dict(window=run.window, scenario=run.scenario, arm=run.arm)
        directory = args.study/run.window/run.scenario/run.arm
        nav = pd.read_csv(directory/'daily.csv')
        nav['valuation_time'] = (pd.to_datetime(nav.date, utc=True)+pd.Timedelta(hours=1)
                                 -pd.Timedelta(microseconds=1))
        navs.append(nav.assign(**tags))
        fills.append(pd.read_csv(directory/'trades.csv').assign(**tags))
        result = json.loads((directory/'result.json').read_text(encoding='utf-8'))
        manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        configs.append(tags | dict(config=result['config'], rules=result['rules'],
            execution=result['execution'], provenance=manifest['provenance'], dataset_id=manifest['dataset_id']))
        for key, order in result['orders'].items():
            orders.append(tags | dict(order_id=key, **order))
        risks.extend(tags | risk for risk in result['risk'])
    pd.concat(navs, ignore_index=True).to_parquet(args.output/'nav.parquet', index=False)
    pd.concat(fills, ignore_index=True).to_csv(args.output/'fills.csv', index=False)
    _write(args.output/'orders.json', orders)
    _write(args.output/'risk-events.json', risks)
    _write(args.output/'configurations.json', configs)
    # Readable checkpoints complement the complete hourly NAV table.
    checkpoints = []
    for frame in navs:
        if frame.window.iloc[0] == 'full' and frame.scenario.iloc[0] == 'base':
            sample = frame.iloc[[0, 719, 1439, len(frame)-1]]
            checkpoints.append(sample[['valuation_time', 'arm', 'equity', 'inventory', 'committed']])
    pd.concat(checkpoints, ignore_index=True).to_csv(args.output/'nav-checkpoints.csv', index=False)
    print(f'Published {len(summary)} runs, {sum(len(n) for n in navs)} NAV rows')


if __name__ == '__main__':
    main()
