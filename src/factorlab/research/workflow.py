"""Offline research lifecycle over the immutable ExperimentTracker store."""
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import shlex
import sys

import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.data.hub import DataHub
from factorlab.data.evidence import sha256
from factorlab.data.research_input import ResearchInputAdapter
from factorlab.dataset.identity import file_hash, json_hash, frame_hash
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.experiments.tracker import SAFETY
from factorlab.research.config import load_config, load_runtime, validate_config, read_json, EXECUTION as CONTRACT
from factorlab.strategies.custom import run_custom_strategy
from factorlab.strategies.robustness import account_batch

ROOT = Path(__file__).resolve().parents[3]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def _source_identity():
    return {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p)
            for p in sorted((ROOT / 'src/factorlab').rglob('*.py'))}


def _inputs(config, runtime):
    adapter = ResearchInputAdapter(runtime['research_root'], runtime['source_root'], config['data']['manifest_sha256'])
    hub = DataHub({'research': adapter})
    research = config['research']
    windows = research['windows'] if config['task'] == 'p6_r01' else [research]
    samples = []
    for w in windows:
        observed = adapter.bars.loc[adapter.bars.symbol.eq(w['symbol']) & adapter.bars.date.between(w['warmup_start'], w['end']), 'date']
        if observed.empty:
            raise ValueError('declared research window has no observed data')
        first, last = str(observed.min().date()), str(observed.max().date())
        if config['task'] == 'p6_r01' and (w['query_start'], w['query_end']) != (first, last):
            raise ValueError('query must retain every observed session in declared window')
        query = dict(symbol=w['symbol'], start=first, end=last)
        bars = hub.read('research', 'daily_bars', **query).data
        samples.append(dict(w, bars=bars, calendar=pd.DatetimeIndex(bars.date)))
    if config['task'] == 'custom_daily':
        if file_hash(runtime['strategy_file']) != research['strategy']['sha256']:
            raise ValueError('strategy source identity changed')
        # The D01 adapter has already checked the upstream snapshot identity.
        # Preserve genuine vendor adjusted data for the existing custom engine.
        payload = (Path(runtime['source_root']) / 'bars.parquet').read_bytes()
        if sha256(payload) != adapter.manifest['bars_sha256']:
            raise ValueError('consumed custom bars identity mismatch')
        source = pd.read_parquet(BytesIO(payload))
        w = research
        source = source.loc[source.symbol.eq(w['symbol']) & pd.to_datetime(source.date).between(w['warmup_start'], w['end'])].copy()
        if 'adj_close' not in source:
            raise ValueError('custom_daily requires genuine adj_close in the pinned upstream snapshot')
        samples[0]['bars'] = source
    return samples


def _definition(config):
    research = config['research']
    windows = research['windows'] if config['task'] == 'p6_r01' else [research]
    return dict(model=config['task'], parameters=config, factors=[], dataset=config['data'],
                label=dict(return_basis='RAW_PRICE_RETURN_NET_OF_TRADING_COSTS', total_return_status='TOTAL_RETURN_UNAVAILABLE'),
                windows=dict(train=None, validation=None, test=[min(w['start'] for w in windows), max(w['end'] for w in windows)]),
                walk_forward=dict(mode='fixed_predeclared_no_fit', windows=[
                    {k: w[k] for k in ('symbol', 'start', 'end', 'warmup_start')} for w in windows]), costs=config['fees'])


def _execute(config, runtime, samples, output):
    research = config['research']
    if config['task'] == 'custom_daily':
        strategy = research['strategy']
        executable = dict(strategy=dict(strategy, file=runtime['strategy_file']),
            research={k: research[k] for k in ('symbol', 'start', 'end', 'initial_cash', 'max_weight', 'board_lot')})
        _save(output / 'engine-config.json', executable)
        result = run_custom_strategy(samples[0]['bars'], samples[0]['calendar'], output / 'engine-config.json',
            fee_schedule=FeeSchedule.a_share_default(**config['fees']), source_kind=config['data']['source_kind'])
        evaluations = {name: value.to_dict() for name, value in result['evaluations'].items()}
        frames = {'signals': result['signals']}
        for name in ('strategy', 'benchmark'):
            frames.update({name + '_' + key: result[name][key] for key in ('nav', 'trades', 'rejections')})
        metrics = {name + ':' + key: metric.value for name, value in result['evaluations'].items() for key, metric in value.metrics.items()}
        summary = dict(status='SOFTWARE_ONLY' if config['data']['source_kind'] == 'SYNTHETIC' else 'EXPLORATORY_REAL_DATA', evaluations=evaluations)
    else:
        template = dict(common=research['common'], strategies=research['strategies'],
            signal_price_column=research['signal_price_column'], fees=config['fees'], manifest_sha256=config['data']['manifest_sha256'])
        result = account_batch(samples, template, source_kind=config['data']['source_kind'])
        frames = dict(observations=result['observations'], **result['tables'])
        evaluations = {}
        for (symbol, year), account in result['results'].items():
            for name, evaluation in account['evaluations'].items():
                prefix = f'{symbol}-{year}-{name}'
                evaluations[prefix] = evaluation.to_dict()
                frames[prefix + '-signals'] = account['signals'][name]
                frames.update({prefix + '-' + key: account['legs'][name][key] for key in ('nav', 'trades', 'rejections')})
        metrics = {str(row.strategy) + ':mean_return': float(row.mean_return) for row in result['tables']['overall'].itertuples()}
        summary = dict(status=result['status'], accounts=len(samples), evaluations=evaluations,
                       complete_historical_p6_r01_batch=False)
    for name, frame in frames.items():
        frame = frame.copy()
        for column in frame.select_dtypes(include='object'):
            if frame[column].map(lambda v: isinstance(v, (dict, list))).any():
                frame[column] = frame[column].map(lambda v: json.dumps(v, sort_keys=True, allow_nan=False))
        frame.to_parquet(output / (name + '.parquet'), index=False)
    summary.update(total_return=None, total_return_status='TOTAL_RETURN_UNAVAILABLE',
        return_basis='RAW_PRICE_RETURN_NET_OF_TRADING_COSTS', financial_admission=False,
        statistics='DESCRIPTIVE_ONLY; NO_PROFITABILITY_CERTIFICATION', safety=SAFETY)
    _save(output / 'results.json', summary)
    return metrics


def _artifacts(output):
    return {p.name: dict(path=str(p.resolve()), sha256=file_hash(p)) for p in sorted(output.iterdir()) if p.is_file()}


def _report(output, config, lifecycle, metrics, command, store, run_id):
    artifacts = _artifacts(output)
    lines = ['# Offline research report', '', ' / '.join(SAFETY), '',
        '## Goal and scope', f"Task: `{config['task']}`. Fixed predeclared research, not financial admission.",
        'Independent security-year accounts are not a shared-capital portfolio.', '',
        '## Data source', '```json', json.dumps(config['data'], indent=2), '```', '',
        '## Actual configuration and execution constraints', '```json', json.dumps(dict(config=config, execution=CONTRACT), indent=2), '```', '',
        '## Lifecycle and key results', '```json', json.dumps(dict(lifecycle=lifecycle, metrics=metrics), indent=2), '```', '',
        '## Costs and evaluation',
        'Existing ResearchEvaluation: daily 252-session annualization, risk-free rate 0; execution uses original engines and dated A-share fee rules.',
        'Stamp duty: 10 bps sell through 2023-08-27, 5 bps thereafter. These are research assumptions, not certified broker schedules.',
        'RAW_PRICE_RETURN_NET_OF_TRADING_COSTS; TOTAL_RETURN_UNAVAILABLE / null. No cash-distribution reinvestment or tax-adjusted total return claim.', '',
        '## Research limitations',
        'Retrospective observed sessions do not establish historical PIT, exchange-calendar completeness, or verified HFQ.',
        'Descriptive statistics are not statistical inference or stable strategy advantage. Simulated execution is not live trading capability.',
        'Workflow completion does not grant formal financial admission. Negative returns remain completed research results.',
        'Trusted local custom Python is executed as code, not sandboxed; offline replay performs no automatic network retrieval.', '',
        '## Artifact index', '| Artifact | SHA256 |', '|---|---|']
    lines.extend(f"| {name} | `{a['sha256']}` |" for name, a in artifacts.items())
    lines.extend(['', 'The immutable tracker terminal receipt binds this report SHA256 as well.', '',
        '## Reproduction commands', 'Original invocation:', '```text', shlex.join(command), '```',
        'Identity and artifact verification:', '```text',
        shlex.join([sys.executable, '-I', str(ROOT / 'tools/research/workflow.py'), 'verify', '--store', str(Path(store).resolve()), '--run-id', run_id]),
        '```', 'To relocate inputs, pass --runtime with a new local binding. Explicit replay requires a new output and run ID:', '```text',
        shlex.join([sys.executable, '-I', str(ROOT / 'tools/research/workflow.py'), 'replay', '--store', str(Path(store).resolve()), '--run-id', run_id,
                    '--runtime', str(output / 'runtime.json'), '--output', str(output.parent / (output.name + '-replay')), '--new-run-id', run_id + '-replay']), '```', ''])
    (output / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


def run(config_path, runtime_path, *, output, store, experiment_id, run_id, command=None, _replay_of=None):
    """Append immutable start/terminal receipts; return the terminal tracker record."""
    config = load_config(config_path)
    runtime = load_runtime(runtime_path, config['task'])
    output = Path(output).resolve()
    tracker = ExperimentTracker(store)
    # Reserve identities before engine execution; no existing receipt is replaced.
    for identifier in (run_id, run_id + '-start'):
        if (tracker.root / 'runs' / (identifier + '.json')).exists():
            raise FileExistsError(identifier)
    if (tracker.root / 'experiments' / (experiment_id + '.json')).exists():
        if tracker.get_experiment(experiment_id)['payload'] != _definition(config):
            raise ValueError('existing experiment definition differs')
    else:
        tracker.create_experiment(experiment_id, _definition(config))
    output.mkdir(parents=True, exist_ok=False)
    provenance = capture_provenance(ROOT)
    provenance['code']['source_sha256'] = _source_identity()
    command = list(command or [sys.executable, '-I', str(ROOT / 'tools/research/workflow.py'), 'run',
        '--config', str(Path(config_path).resolve()), '--runtime', str(Path(runtime_path).resolve()), '--output', str(output),
        '--store', str(tracker.root), '--experiment-id', experiment_id, '--run-id', run_id])
    lifecycle = dict(status='RUNNING', stage='PREPARATION', started_at=_now(), ended_at=None,
                     reason=None, unfinished_stages=['PREPARATION', 'INPUT_VALIDATION', 'EXECUTION', 'REPORT'])
    validation = dict(workflow_schema=1, config_sha256=json_hash(config), lifecycle=lifecycle.copy(), safety=SAFETY,
                      execution=CONTRACT, sample_hash=None, evaluation=None)
    tracker.append_run(experiment_id, run_id + '-start', **provenance, command=command, status='RUNNING',
                       qualification=config['data']['source_kind'], validation=validation)
    metrics = {}
    status = 'FAILED'
    try:
        _save(output / 'config.json', config)
        _save(output / 'runtime.json', runtime)
        lifecycle.update(stage='INPUT_VALIDATION', unfinished_stages=['INPUT_VALIDATION', 'EXECUTION', 'REPORT'])
        samples = _inputs(config, runtime)
        validation['sample_hash'] = json_hash([dict(symbol=s['symbol'], start=s['start'], end=s['end'], bars=frame_hash(s['bars'])) for s in samples])
        validation['evaluation'] = dict(task=config['task'], contract=CONTRACT, periods_per_year=252, risk_free_rate=0)
        lifecycle.update(stage='EXECUTION', unfinished_stages=['EXECUTION', 'REPORT'])
        metrics = _execute(config, runtime, samples, output)
        if _replay_of is not None:
            lifecycle.update(stage='REPLAY_VERIFICATION', unfinished_stages=['REPLAY_VERIFICATION', 'REPORT'])
            _verify_replay(_replay_of, output)
            validation['replay_status'] = 'REPLAY_VERIFIED'
        lifecycle.update(stage='REPORT', unfinished_stages=['REPORT'])
        status = 'SUCCEEDED'
        lifecycle.update(status='COMPLETED', stage='COMPLETE', unfinished_stages=[])
    except FileNotFoundError as exc:
        missing_input = lifecycle['stage'] == 'INPUT_VALIDATION'
        lifecycle.update(status='DATA_UNAVAILABLE' if missing_input else 'EXECUTION_FAILED', reason=str(exc))
        status = 'NOT_RUN' if missing_input else 'FAILED'
    except Exception as exc:
        failure = {'INPUT_VALIDATION': 'INPUT_REJECTED', 'REPLAY_VERIFICATION': 'REPLAY_MISMATCH'}.get(lifecycle['stage'], 'EXECUTION_FAILED')
        lifecycle.update(status=failure, reason=f'{type(exc).__name__}: {exc}')
    lifecycle['ended_at'] = _now()
    validation['lifecycle'] = lifecycle
    try:
        _save(output / 'lifecycle.json', lifecycle)
        _report(output, config, lifecycle, metrics, command, store, run_id)
    except Exception as exc:
        status = 'FAILED'
        lifecycle.update(status='REPORT_FAILED', stage='REPORT', unfinished_stages=['REPORT'],
                         reason=f'{type(exc).__name__}: {exc}', ended_at=_now())
    return tracker.append_run(experiment_id, run_id, **provenance, command=command, status=status,
        qualification=config['data']['source_kind'], portfolio_metrics=metrics, artifacts=_artifacts(output), validation=validation)


def inspect(store, run_id):
    """Read any existing ExperimentTracker run without inventing a second index."""
    return ExperimentTracker(store).get_run(run_id)


def compare(store, run_ids):
    return ExperimentTracker(store).compare(run_ids)


def verify(store, run_id, *, runtime_path=None):
    """Verify saved receipts, artifacts, effective config, code and current inputs."""
    tracker = ExperimentTracker(store)
    saved = tracker.reproduction(run_id, verify_artifacts=True)
    run_record = saved['run']
    if run_record['status'] != 'SUCCEEDED':
        raise ValueError('cannot verify reproducible success for a non-successful run')
    validation = run_record['validation']
    if validation.get('workflow_schema') != 1:
        raise ValueError('legacy run supports tracker reproduction only, not unified replay')
    config = validate_config(saved['definition']['parameters'])
    if json_hash(config) != validation['config_sha256'] or read_json(run_record['artifacts']['config.json']['path']) != config:
        raise ValueError('configuration identity mismatch')
    current_provenance = capture_provenance(ROOT)
    for key in ('python_version', 'package_versions'):
        if current_provenance['environment'][key] != run_record['environment'][key]:
            raise ValueError('runtime environment identity mismatch: ' + key)
    current_code = current_provenance['code']
    for key in ('commit', 'tree'):
        if current_code[key] != run_record['code'][key]:
            raise ValueError('code identity mismatch: ' + key)
    if _source_identity() != run_record['code']['source_sha256']:
        raise ValueError('executed source identity mismatch')
    if runtime_path is None:
        runtime_path = run_record['artifacts']['runtime.json']['path']
    runtime = load_runtime(runtime_path, config['task'])
    samples = _inputs(config, runtime)
    sample_hash = json_hash([dict(symbol=s['symbol'], start=s['start'], end=s['end'], bars=frame_hash(s['bars'])) for s in samples])
    if sample_hash != validation['sample_hash']:
        raise ValueError('actual sample identity mismatch')
    return dict(verified=True, status='IDENTITY_VERIFIED', run_id=run_id, safety=SAFETY)


def replay(store, run_id, *, runtime_path, output, new_run_id):
    """Re-execute local inputs and compare deterministic results using numeric tolerances."""
    verify(store, run_id, runtime_path=runtime_path)
    record = inspect(store, run_id)
    original = record['payload']['run']
    config_path = original['artifacts']['config.json']['path']
    repeated = run(config_path, runtime_path, output=output, store=store, experiment_id=record['experiment_id'],
                   run_id=new_run_id, _replay_of=original)
    if repeated['payload']['run']['status'] != 'SUCCEEDED':
        raise ValueError('replay failed; see terminal receipt ' + new_run_id)
    return dict(verified=True, status='REPLAY_VERIFIED', run_id=new_run_id, safety=SAFETY)


def _verify_replay(original, output):
    for name, artifact in original['artifacts'].items():
        if name.endswith('.parquet'):
            pd.testing.assert_frame_equal(pd.read_parquet(artifact['path']), pd.read_parquet(output / name),
                                          check_exact=False, rtol=1e-10, atol=1e-12)
    _compare_results(read_json(original['artifacts']['results.json']['path']), read_json(output / 'results.json'))


def _compare_results(expected, actual):
    import math
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or expected.keys() != actual.keys():
            raise ValueError('result fields differ')
        for key in expected:
            _compare_results(expected[key], actual[key])
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError('result list differs')
        for a, b in zip(expected, actual):
            _compare_results(a, b)
    elif type(expected) in (int, float) and type(actual) in (int, float):
        if not math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-12):
            raise ValueError('result number differs')
    elif expected != actual:
        raise ValueError('result value differs')
