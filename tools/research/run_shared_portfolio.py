"""Run the fixed P8-01 annual accounts; keep full execution receipts private."""
import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import pandas as pd
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.data.research_input import ResearchInputAdapter
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.real_shared_portfolio import InputBlocked, execute, evaluate, validate_panel

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description='P8-01: NO LIVE MONEY; private annual shared accounts')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--research-root', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    frozen_path = ROOT / config['frozen_sample']
    frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
    counts = {str(y): n for y, n in Counter(w['year'] for w in frozen['windows']).items()}
    if counts != config['annual_counts'] or frozen['manifest_sha256'] != config['manifest_sha256']:
        raise SchemaError('frozen sample counts or source identity mismatch')
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / 'config.json', config)
    git = lambda *a: subprocess.check_output(['git', *a], cwd=ROOT, text=True).strip()
    code = dict(commit=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'), dirty=bool(git('status', '--porcelain')))
    implementation = {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p)
                      for p in sorted((ROOT / 'src/factorlab').rglob('*.py'))}
    implementation[str(Path(__file__).relative_to(ROOT)).replace('\\', '/')] = file_hash(Path(__file__))
    summary = dict(task='P8-01', status='P8_01_BLOCKED_DATA', safety=config['safety'], annual_counts=counts,
        annual_accounts='INDEPENDENT_NOT_CONTINUOUS', return_basis=config['return_basis'],
        total_return_status='TOTAL_RETURN_UNAVAILABLE', total_return=None, market_index_status='UNAVAILABLE',
        execution_reference=dict(symbol=config['execution_reference_symbol'], kind=config['execution_reference_kind']),
        manifest_sha256=config['manifest_sha256'], bars_sha256=config['bars_sha256'],
        config_sha256=file_hash(args.config), frozen_sample_sha256=file_hash(frozen_path),
        code=code, implementation_sha256=implementation,
        environment=dict(python=sys.version, platform=platform.platform(), pandas=pd.__version__), years={})
    try:
        adapter = ResearchInputAdapter(args.research_root, args.source_root, config['manifest_sha256'])
        if adapter.manifest['bars_sha256'] != config['bars_sha256']:
            raise SchemaError('predeclared raw snapshot identity mismatch')
    except (FileNotFoundError, SchemaError) as exc:
        summary['blocked_reason'] = str(exc)
        save(args.output / 'summary.json', summary)
        print(json.dumps(dict(status=summary['status'], reason=str(exc)), ensure_ascii=False))
        return 2
    hub = DataHub({'research': adapter})
    for year in sorted(map(int, config['annual_counts'])):
        windows = [w for w in frozen['windows'] if w['year'] == year]
        symbols = sorted(w['symbol'] for w in windows)
        if len(set(symbols)) != len(symbols):
            raise SchemaError('duplicate frozen security/year')
        calendar = pd.DatetimeIndex(sorted(adapter.bars.loc[adapter.bars.date.dt.year.eq(year), 'date'].unique()))
        try:
            parts = [hub.read('research', 'daily_bars', symbol=w['symbol'], start=w['query_start'], end=w['query_end']).data for w in windows]
            history = pd.concat(parts, ignore_index=True)
            observed = pd.DatetimeIndex(sorted(adapter.bars.loc[adapter.bars.date.between(windows[0]['query_start'], windows[0]['query_end']), 'date'].unique()))
            validate_panel(history, observed, symbols)
            reference = hub.read('research', 'daily_bars', symbol=config['execution_reference_symbol'],
                start=str(calendar[0].date()), end=str(calendar[-1].date())).data
            validate_panel(reference, calendar, [config['execution_reference_symbol']])
        except (InputBlocked, UnsupportedQueryError) as exc:
            summary['years'][str(year)] = dict(status='BLOCKED', reason=str(exc), securities=len(symbols))
            continue
        folder = args.output / str(year)
        folder.mkdir()
        annual = dict(status='COMPLETED', securities=len(symbols), sessions=len(calendar),
            first_session=str(calendar[0].date()), last_session=str(calendar[-1].date()),
            input_preflight='ALIGNED_ALL_RAW_OHLC_WITHIN_D01_QUALIFIED_INTERVALS', schemes={})
        for scheme in ('equal_weight', 'momentum'):
            result, targets, decisions, audit = execute(history, calendar, symbols, reference, scheme, config)
            nav, evaluation, metrics = evaluate(result, calendar, config, scheme)
            account = folder / scheme
            account.mkdir()
            for name, frame in dict(nav=nav, trades=result.trades, positions=result.positions,
                    rejections=result.rejections, targets=targets, decisions=decisions,
                    target_comparison=result.target_comparison).items():
                frame.to_csv(account / (name + '.csv'), index=False)
            result.trades[['execution_date', 'symbol', 'side', 'commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage', 'transaction_cost']].to_csv(account / 'fees.csv', index=False)
            save(account / 'evaluation.json', evaluation.to_dict())
            save(account / 'audit.json', audit)
            annual['schemes'][scheme] = dict(metrics=metrics, ledger_audit=audit)
        cash = pd.DataFrame(dict(date=calendar, strategy_nav=1., benchmark_nav=1., strategy_return=0., benchmark_return=0.,
            cash=config['initial_cash'], portfolio_value=config['initial_cash'], holdings=0, transaction_cost=0., turnover=0.))
        cash.to_csv(folder / 'cash-reference.csv', index=False)
        from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
        cash_context = EvaluationContext(dataset_id=config['bars_sha256'], universe_id='P6_R01_FROZEN_ANNUAL',
            label=config['return_basis'], label_id='RAW_PRICE', benchmark_id='MODEL_CASH_ZERO_RETURN_NOT_INDEX',
            calendar=tuple(calendar.strftime('%Y-%m-%d')), holding_days=1, source_kind='REAL_CANDIDATE')
        cash_eval = ResearchEvaluation(cash_context).portfolio('cash', cash, trades=result.trades.iloc[:0], positions=result.positions.iloc[:0])
        save(folder / 'cash-evaluation.json', cash_eval.to_dict())
        annual['schemes']['cash'] = dict(metrics={k: v.value for k, v in cash_eval.metrics.items()},
            meaning='MODEL_INTERNAL_ZERO_INTEREST_REFERENCE_NOT_BANK_OR_FUND_RETURN')
        summary['years'][str(year)] = annual
    if all(y['status'] == 'COMPLETED' for y in summary['years'].values()):
        summary['status'] = 'P8_01_REAL_SHARED_PORTFOLIO_COMPLETED'
    summary['artifact_sha256'] = {str(p.relative_to(args.output)).replace('\\', '/'): file_hash(p)
                                  for p in sorted(args.output.rglob('*')) if p.is_file()}
    save(args.output / 'summary.json', summary)
    tracker = ExperimentTracker(args.output / 'tracker')
    tracker.create_experiment('p8-01', dict(model='fixed_annual_shared_cash_portfolio', parameters=config, factors=[],
        dataset=dict(manifest_sha256=config['manifest_sha256'], frozen_sample_sha256=summary['frozen_sample_sha256']),
        label=dict(return_basis=config['return_basis']), windows=dict(train=None, validation=None, test=['2016-01-01', '2018-12-31']),
        walk_forward=dict(mode='NO_FIT_NO_PARAMETER_SEARCH_INDEPENDENT_ANNUAL'), costs=config['fees']))
    tracker.append_run('p8-01', 'shared', code=code, environment=summary['environment'], command=sys.argv,
        status='SUCCEEDED' if summary['status'].endswith('COMPLETED') else 'FAILED', qualification='REAL_CANDIDATE',
        artifacts=dict(summary=dict(path=str((args.output / 'summary.json').resolve()))),
        validation=dict(safety=config['safety'], status=summary['status']))
    print(json.dumps(dict(status=summary['status'], years=summary['years']), ensure_ascii=False, allow_nan=False))
    return 0 if summary['status'].endswith('COMPLETED') else 2


if __name__ == '__main__':
    raise SystemExit(main())
