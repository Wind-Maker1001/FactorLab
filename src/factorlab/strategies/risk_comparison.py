"""Frozen-account entry-budget comparison using the shared execution engine."""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

from factorlab.data.hub import DataHub
from factorlab.data.research_input import ResearchInputAdapter
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.real_comparison import NAMES, RETURN_BASIS, compare, execute_signals
from factorlab.strategies.robustness import summarize
from factorlab.strategies.robustness_real import save

COMPLETED = 'P7_02_REAL_RISK_COMPARISON_COMPLETED'
BLOCKED = 'P7_02_BLOCKED_DATA'


def budget_signals(signals, budget):
    """Scale original positive entry targets, preserving exits and hold intents."""
    if not np.isfinite(budget) or not 0 < budget <= 1:
        raise SchemaError('entry budget must be within (0, 1]')
    result = {name: frame.copy(deep=True) for name, frame in signals.items()}
    for frame in result.values():
        positive = frame.target_weight.gt(0)
        frame.loc[positive, 'target_weight'] *= budget
    return result


def observations(result, sample, scenario, budget, initial_cash):
    rows = []
    for name in NAMES:
        leg, stats = result['legs'][name], result['summary'][name]
        nav = leg['nav']
        costs = stats['total_transaction_cost'] / initial_cash
        rows.append(dict(symbol=sample['symbol'], year=sample['year'], industry=sample['industry'],
            scenario=scenario, budget=budget, strategy=name, **stats,
            average_capital_exposure=float((nav.market_value / nav.portfolio_value).mean()),
            maximum_capital_exposure=float((nav.market_value / nav.portfolio_value).max()),
            holding_sessions=int(nav.shares.gt(0).sum()), sessions=len(nav),
            cost_to_initial_capital=costs,
            same_fill_gross_contribution=stats['cumulative_return'] + costs,
            no_fill=leg['trades'].empty,
            cash_constraint_rejections=int(leg['rejections'].reason.eq('cash_constraint').sum())))
    return rows


def paired_tables(frame):
    """Same-account benchmark contrasts; all cross-account inference is descriptive."""
    keys = ['scenario', 'symbol', 'year']
    cols = ['cumulative_return', 'maximum_drawdown', 'average_capital_exposure',
            'cost_to_initial_capital', 'same_fill_gross_contribution']
    benchmark = frame.loc[frame.strategy.eq('buy_hold'), keys + cols].rename(
        columns={c: 'benchmark_' + c for c in cols})
    pairs = frame.merge(benchmark, on=keys, validate='many_to_one')
    for col in cols:
        pairs['paired_' + col] = pairs[col] - pairs['benchmark_' + col]
    pairs['cost_reverses_pair_sign'] = (
        pairs.paired_cumulative_return.gt(0) != pairs.paired_same_fill_gross_contribution.gt(0))
    pairs['cost_reverses_absolute_sign'] = (
        pairs.cumulative_return.gt(0) != pairs.same_fill_gross_contribution.gt(0))
    metrics = {c: 'mean' for c in ['paired_' + c for c in cols]}
    metrics.update(cost_reverses_pair_sign='sum', cost_reverses_absolute_sign='sum', no_fill='sum',
                   annualized_volatility='mean', sharpe_ratio='mean', maximum_drawdown='mean',
                   calmar_ratio='mean', maximum_capital_exposure='mean')
    contrasts = pairs.groupby(['scenario', 'strategy']).agg(metrics).reset_index()
    security = pairs.groupby(['scenario', 'strategy', 'symbol']).agg(
        mean_return=('cumulative_return', 'mean'),
        mean_paired_return=('paired_cumulative_return', 'mean'),
        mean_paired_drawdown=('paired_maximum_drawdown', 'mean')).reset_index()
    leave = []
    for (scenario, strategy), group in security.groupby(['scenario', 'strategy']):
        for symbol in group.symbol:
            others = group.loc[group.symbol.ne(symbol)]
            leave.append(dict(scenario=scenario, strategy=strategy, excluded_symbol=symbol,
                securities=len(others), mean_return=others.mean_return.mean(),
                mean_paired_return=others.mean_paired_return.mean(),
                mean_paired_drawdown=others.mean_paired_drawdown.mean()))
    return dict(paired_accounts=pairs, contrasts=contrasts, security_means=security,
                paired_leave_one_security_out=pd.DataFrame(leave))


def match_original(result, folder):
    """Compare actual legacy signal, fill, rejection and NAV values after CSV IO."""
    for name in NAMES:
        frames = dict(signals=result['signals'][name], **result['legs'][name])
        for kind in ('signals', 'nav', 'trades', 'rejections'):
            old = pd.read_csv(folder / f'{name}_{kind}.csv', float_precision='round_trip')
            new = pd.read_csv(StringIO(frames[kind].to_csv(index=False)), float_precision='round_trip')
            pd.testing.assert_frame_equal(new, old, check_dtype=False, rtol=1e-12, atol=1e-9)


def run(config_path, research_root, source_root, baseline_root, output):
    repo = Path(__file__).resolve().parents[3]
    configuration = json.loads(Path(config_path).read_text(encoding='utf-8'))
    output, baseline_root = Path(output), Path(baseline_root)
    frozen_path = repo / configuration['frozen_sample']
    frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
    if frozen['manifest_sha256'] != configuration['manifest_sha256']:
        raise SchemaError('frozen sample manifest differs')
    # All required private roots must be available before the first backtest.
    required = [Path(research_root) / 'manifest.json', Path(source_root) / 'bars.parquet',
                baseline_root / 'frozen-sample.json', baseline_root / 'observations.csv']
    required += [baseline_root / f"{w['symbol']}-{w['year']}" / f'{name}_{kind}.csv'
                 for w in frozen['windows'] for name in NAMES
                 for kind in ('signals', 'nav', 'trades', 'rejections')]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        output.mkdir(parents=True, exist_ok=False)
        save(output / 'config.json', configuration)
        receipt = dict(status=BLOCKED, missing_inputs=missing, real_accounts_run=0,
                       scope=configuration['scope'], safety=configuration['safety'])
        save(output / 'summary.json', receipt)
        return receipt
    original_sample = json.loads((baseline_root / 'frozen-sample.json').read_text(encoding='utf-8'))
    if original_sample != frozen:
        raise SchemaError('original experiment sample differs')
    adapter = ResearchInputAdapter(research_root, source_root, configuration['manifest_sha256'])
    hub = DataHub({'research': adapter})
    template = json.loads((repo / configuration['parameter_source']).read_text(encoding='utf-8'))
    template['manifest_sha256'] = configuration['manifest_sha256']
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'config.json', configuration)
    save(output / 'frozen-sample.json', frozen)
    rows, baseline_identities = [], {}
    for sample in frozen['windows']:
        symbol, year = sample['symbol'], sample['year']
        bars = hub.read('research', 'daily_bars', symbol=symbol,
                        start=sample['query_start'], end=sample['query_end']).data
        config = deepcopy(template)
        config['common'].update(symbol=symbol, start=sample['start'], end=sample['end'])
        config['warmup_start'] = sample['warmup_start']
        base = compare(bars, pd.DatetimeIndex(bars.date), config, source_kind='REAL_CANDIDATE')
        folder = baseline_root / f'{symbol}-{year}'
        match_original(base, folder)
        baseline_identities.update({str(p.relative_to(baseline_root)): file_hash(p)
            for p in folder.glob('*.csv')})
        scenarios = [('original', 1.0)] + [(f'budget_{b:g}', b) for b in configuration['budgets']]
        for scenario, budget in scenarios:
            result = base if scenario == 'original' else execute_signals(
                budget_signals(base['signals'], budget), base['input_bars'], base['research'],
                config, source_kind='REAL_CANDIDATE')
            rows.extend(observations(result, sample, scenario, budget, config['common']['initial_cash']))
            target = output / scenario / f'{symbol}-{year}'
            target.mkdir(parents=True)
            for name in NAMES:
                result['signals'][name].to_csv(target / f'{name}_signals.csv', index=False)
                for kind in ('nav', 'trades', 'rejections'):
                    result['legs'][name][kind].to_csv(target / f'{name}_{kind}.csv', index=False)
    frame = pd.DataFrame(rows)
    frame.to_csv(output / 'account-metrics.csv', index=False)
    for scenario, part in frame.groupby('scenario'):
        for name, table in summarize(part).items():
            table.to_csv(output / scenario / f'{name}.csv', index=False)
    for name, table in paired_tables(frame).items():
        table.to_csv(output / f'{name}.csv', index=False)
    git = lambda *args: subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    code = dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'),
                dirty=bool(git('status', '--porcelain')))
    environment = dict(python=sys.version, platform=platform.platform(), pandas=pd.__version__, numpy=np.__version__)
    sources = [repo / 'src/factorlab/strategies' / name for name in
               ['risk_comparison.py', 'real_comparison.py', 'technical.py', 'dual_ma.py', 'robustness.py']]
    sources += [repo / 'src/factorlab' / name for name in
                ['backtest/target.py', 'backtest/execution.py', 'backtest/fees.py',
                 'evaluation/research.py', 'experiments/tracker.py', 'data/research_input.py']]
    receipt = dict(status=COMPLETED, scope=configuration['scope'], safety=configuration['safety'],
        real_accounts_run=len(frozen['windows']), result_rows=len(frame), baseline_match='ALL_SIGNALS_NAV_TRADES_REJECTIONS',
        configuration=configuration, strategy_parameters=template, return_basis=RETURN_BASIS,
        total_return_status='TOTAL_RETURN_UNAVAILABLE', code=code, environment=environment,
        frozen_sample_sha256=file_hash(frozen_path), source_manifest_sha256=configuration['manifest_sha256'],
        raw_bars_sha256=adapter.manifest['bars_sha256'],
        original_summary_sha256=file_hash(baseline_root / 'summary.json'),
        original_artifacts_sha256=baseline_identities,
        implementation_sha256={p.relative_to(repo).as_posix(): file_hash(p) for p in sources},
        command=sys.argv)
    save(output / 'summary.json', receipt)
    tracker = ExperimentTracker(output / 'tracker')
    tracker.create_experiment('p7-02', dict(model='six_original_signals_fixed_entry_budgets',
        parameters=configuration, factors=[], dataset=dict(manifest_sha256=configuration['manifest_sha256'],
        frozen_sample=receipt['frozen_sample_sha256']), label=dict(return_basis=RETURN_BASIS),
        windows=dict(train=None, validation=None, test=['2016-01-01', '2018-12-31']),
        walk_forward=dict(mode='fixed_no_fit'), costs=template['fees']))
    tracker.append_run('p7-02', 'comparison', code=code, environment=environment, command=sys.argv,
        status='SUCCEEDED', qualification='REAL_CANDIDATE',
        artifacts=dict(summary=dict(path=str((output / 'summary.json').resolve()))),
        validation=dict(status=COMPLETED, baseline_match=receipt['baseline_match'], safety=configuration['safety']))
    return receipt
