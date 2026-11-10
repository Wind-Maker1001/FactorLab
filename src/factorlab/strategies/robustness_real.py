"""Pinned D01 consumption and private independent-account research artifacts."""
from copy import deepcopy
import json
from pathlib import Path
import platform
import subprocess
import sys

import pandas as pd

from factorlab.data.hub import DataHub
from factorlab.data.research_input import ResearchInputAdapter
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.robustness import account_batch
from factorlab.strategies.real_comparison import NAMES, RETURN_BASIS
from factorlab.strategies.technical_research import SAFETY


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def freeze(configuration, adapter):
    """Determine eligibility from coverage only, before consuming any returns."""
    windows, exclusions = [], []
    sessions = pd.DatetimeIndex(sorted(adapter.bars.date.unique()))
    for industry, symbols in configuration['candidates'].items():
        for symbol in symbols:
            entry = next(r for r in adapter.quality['securities'] if r['symbol'] == symbol)
            for year in configuration['years']:
                start, end, warmup = f'{year}-01-01', f'{year}-12-31', f'{year-1}-01-01'
                required = sessions[(sessions >= warmup) & (sessions <= end)]
                prior = required[required.year == year-1]
                research = required[required.year == year]
                eligible = len(prior) >= 50 and len(research) >= 240 and any(
                    r['start'] <= str(required[0].date()) and str(required[-1].date()) <= r['end']
                    for r in entry['intervals'])
                row = dict(symbol=symbol, industry=industry, year=year, start=start, end=end, warmup_start=warmup)
                if eligible:
                    row.update(query_start=str(required[0].date()), query_end=str(required[-1].date()))
                    windows.append(row)
                else:
                    exclusions.append(dict(**row, reason='MISSING_PREVIOUS_YEAR_WARMUP' if len(prior) < 50 else 'REJECTED_SESSION_IN_WARMUP_OR_RESEARCH'))
    return windows, exclusions


def run(config_path, research_root, source_root, output):
    config_path, output = Path(config_path), Path(output)
    configuration = json.loads(config_path.read_text(encoding='utf-8'))
    adapter = ResearchInputAdapter(research_root, source_root, configuration['manifest_sha256'])
    windows, exclusions = freeze(configuration, adapter)
    if not windows:
        raise SchemaError('no qualified complete annual windows with prior-year warmup')
    repo = Path(__file__).resolve().parents[3]
    pinned_sample = json.loads((repo / 'docs/validation/p6-r01/frozen-sample.json').read_text(encoding='utf-8'))
    if windows != pinned_sample['windows'] or exclusions != pinned_sample['exclusions'] or configuration['manifest_sha256'] != pinned_sample['manifest_sha256']:
        raise SchemaError('sample differs from predeclared frozen coverage')
    template = json.loads((repo / configuration['parameter_source']).read_text(encoding='utf-8'))
    template['manifest_sha256'] = configuration['manifest_sha256']
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'config.json', configuration)
    save(output / 'frozen-sample.json', dict(windows=windows, exclusions=exclusions,
        manifest_sha256=configuration['manifest_sha256'], completion_policy=configuration['completion_policy']))
    hub = DataHub({'research': adapter})
    samples = []
    for window in windows:
        query = dict(symbol=window['symbol'], start=window['query_start'], end=window['query_end'])
        bars = hub.read('research', 'daily_bars', **query).data
        samples.append(dict(**window, bars=bars, calendar=pd.DatetimeIndex(bars.date)))
    result = account_batch(samples, template, source_kind='REAL_CANDIDATE')
    observations = result['observations']
    sufficient = observations.symbol.nunique() >= 12 and observations.year.nunique() >= 3 and observations.industry.nunique() >= 4
    status = 'P6_R01_REAL_ROBUSTNESS_COMPLETED' if sufficient else 'P6_R01_PARTIAL'
    observations.to_json(output / 'observations.json', orient='records', indent=2)
    observations.to_csv(output / 'observations.csv', index=False)
    for name, table in result['tables'].items():
        table.to_csv(output / (name + '.csv'), index=False)
    for (symbol, year), account in result['results'].items():
        folder = output / f'{symbol}-{year}'
        folder.mkdir()
        for name in NAMES:
            for kind in ('nav', 'trades', 'rejections'):
                account['legs'][name][kind].to_csv(folder / f'{name}_{kind}.csv', index=False)
            account['signals'][name].to_csv(folder / f'{name}_signals.csv', index=False)
            save(folder / f'{name}_evaluation.json', account['evaluations'][name].to_dict())
    git = lambda *args: subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    code = dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'), dirty=bool(git('status','--porcelain')))
    environment = dict(python=sys.version, platform=platform.platform(), pandas=pd.__version__)
    summary = dict(status=status, scope='EXPLORATORY_REAL_DATA', safety=SAFETY,
        return_basis=RETURN_BASIS, total_return=None, total_return_status='TOTAL_RETURN_UNAVAILABLE',
        source_manifest_sha256=configuration['manifest_sha256'], real_accounts_run=len(windows),
        securities=int(observations.symbol.nunique()), years=sorted(observations.year.unique().tolist()),
        frozen_sample=file_hash(output / 'frozen-sample.json'), code=code, environment=environment,
        strategy_parameters=template, tables={k:json.loads(v.to_json(orient='records')) for k,v in result['tables'].items()},
        implementation_sha256={p.name:file_hash(p) for p in [Path(__file__), Path(__file__).with_name('robustness.py'), Path(__file__).with_name('real_comparison.py')]})
    save(output / 'summary.json', summary)
    tracker = ExperimentTracker(output / 'tracker')
    tracker.create_experiment('p6-r01', dict(model='six_fixed_raw_price_independent_annual_accounts',
        parameters=template, factors=[], dataset=dict(manifest_sha256=configuration['manifest_sha256'], frozen_sample=summary['frozen_sample']),
        label=dict(return_basis=RETURN_BASIS), windows=dict(train=None, validation=None, test=['2016-01-01','2018-12-31']),
        walk_forward=dict(mode='fixed_parameters_external_time_comparison_no_fit'), costs=template['fees']))
    tracker.append_run('p6-r01','robustness', code=code, environment=environment, command=sys.argv,
        status='SUCCEEDED', qualification='REAL_CANDIDATE', artifacts=dict(summary=dict(path=str((output/'summary.json').resolve()))),
        validation=dict(safety=SAFETY, independent_accounts=True, status=status))
    return summary
