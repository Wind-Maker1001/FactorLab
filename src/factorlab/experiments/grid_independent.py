"""Frozen independent-sample matrix on the existing authoritative Paper runner."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
from time import perf_counter, process_time

import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_lifecycle import lifecycle_metrics
from factorlab.experiments.grid_paper import D, _write, run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


GENERATION = 'bde00b2f948842e2b8c2a00465a9f8c6'
BARS_SHA256 = '16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab'
MANIFEST_SHA256 = 'a689366daf5815f4fea2726fa213197d5505ae89548083a094d39476aae3e12c'
PREREGISTRATION_SHA256 = '7a599b39d13f8cc009a8a37ecc82c3375e09ed587d4997684e39853767fcc43c'
FROZEN_SOURCES_SHA256 = '0989eab0a86c33854636a78af6b6e739baca643d02af61b5cf3290d32fdd7109'
VERSION = '352d960fad24e50f1423ff173d838a994a29603d2629652f46c7edc76e5e2a83'
BOUNDARIES = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


class StudyBlocked(ValueError):
    """A required independent research admission fact is missing."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def locked_json(root, relative, expected):
    """Only checkout newline translation may differ from the locked Git bytes."""
    data = (Path(root) / relative).read_bytes()
    lf = data.replace(b'\r\n', b'\n')
    if expected not in {digest(data), digest(lf), digest(lf.replace(b'\n', b'\r\n'))}:
        raise StudyBlocked(f'frozen content digest mismatch: {relative}')
    return json.loads(data)


def load_design(repository):
    root = Path(repository)
    lock = json.loads((root / 'docs/validation/p7-r04a/preregistration-lock.json').read_text(encoding='utf-8'))
    if lock['sha256'] != PREREGISTRATION_SHA256:
        raise StudyBlocked('preregistration lock identity mismatch')
    prereg = locked_json(root, lock['path'], PREREGISTRATION_SHA256)
    sources = json.loads((root / 'docs/validation/p7-r04a/verified-assets.json').read_text(encoding='utf-8'))['frozen_sources']
    source_hashes = {path: receipt['sha256'] for path, receipt in sources.items()}
    if digest(json.dumps(source_hashes, sort_keys=True, separators=(',', ':')).encode()) != FROZEN_SOURCES_SHA256:
        raise StudyBlocked('frozen source mapping digest mismatch')
    frozen = {path: locked_json(root, path, expected) for path, expected in source_hashes.items()}
    lifecycle = frozen[prereg['strategy_source']]
    config = GridConfig(**frozen[lifecycle['base_config']])
    rules = GridRules(**{k: D(v) for k, v in frozen[lifecycle['rules']].items()})
    return prereg, lifecycle, config, rules, {lock['path']: PREREGISTRATION_SHA256} | {
        path: receipt['sha256'] for path, receipt in sources.items()}


def load_sample(snapshot):
    snapshot = Path(snapshot)
    if snapshot.name != GENERATION:
        raise StudyBlocked('snapshot generation mismatch')
    data = (snapshot / 'manifest.json').read_bytes()
    if digest(data) != MANIFEST_SHA256:
        raise StudyBlocked('snapshot manifest digest mismatch')
    manifest = json.loads(data)
    if manifest['data_version'] != VERSION or manifest['tables']['bars']['sha256'] != BARS_SHA256:
        raise StudyBlocked('snapshot content identity mismatch')
    try:
        bars, identity = load_hourly(snapshot)
    except ValueError as error:
        raise StudyBlocked(str(error)) from error
    if (len(bars) != 2160 or identity['start'] != '2026-05-14T00:00:00+00:00'
            or identity['end_exclusive'] != '2026-08-12T00:00:00+00:00'):
        raise StudyBlocked('frozen independent window mismatch')
    identity.update(generation=GENERATION, manifest_sha256=MANIFEST_SHA256,
                    source=manifest['source'], external_raw_receipts=manifest['external_raw_receipts'])
    return bars, identity


def account_matrix(prereg):
    """Stable order, with both costs independent of any economic outcome."""
    return [(scenario, arm, mode) for scenario in ('base', 'stress')
            for arm, mode in [('cash', 'arithmetic'), ('fixed_budget_buy_hold', 'arithmetic')]
            + [(arm, mode) for arm in ('B', 'D') for mode in prereg['modes']]]


def account_identity(hashes, identity, scenario, arm, mode):
    payload = dict(study='P7-R04B-E01', frozen=hashes, generation=identity['generation'],
                   bars_sha256=identity['bars_sha256'], scenario=scenario, arm=arm, mode=mode)
    return 'p7-r04b-e01-' + digest(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())[:24]


def require_admission(repository):
    """The checked-in admission record must contain verified source receipts."""
    root = Path(repository)
    admission = json.loads((root / 'docs/validation/p7-r04b-e01/admission.json').read_text(encoding='utf-8'))
    if not admission['independence']['not_used_for_parameter_development_or_profit_selection']:
        raise StudyBlocked('independent provenance declaration missing')
    listing = admission['listing_cross_check']
    if listing['status'] != 'VERIFIED':
        raise StudyBlocked('official listing announcement / precise first-trade cross-check unresolved')
    if not listing['receipts']:
        raise StudyBlocked('listing corroboration receipts missing')
    for receipt in listing['receipts']:
        if digest((root / receipt['path']).read_bytes()) != receipt['sha256']:
            raise StudyBlocked('listing corroboration receipt digest mismatch')
    return admission


def execute_matrix(output, *, bars, identity, prereg, lifecycle, config, rules, hashes):
    """Execute an admitted design; production callers must use run_study."""
    from factorlab.experiments.grid_fifo import attribution

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    wall, cpu = perf_counter(), process_time()
    _write(output / 'design.json', dict(preregistration=prereg, frozen_sha256=hashes,
        accounts=account_matrix(prereg), research_level='ASSUMPTION_BOUND_PAPER_RESEARCH'))
    _write(output / 'data-identity.json', identity)
    rows = []
    for scenario, arm, mode in account_matrix(prereg):
        fee, slip = prereg['cost_scenarios_bps'][scenario]
        reseed, breakout = prereg['lifecycle_arms'].get(arm, [False, 'pause'])
        settings = replace(config, mode=mode, breakout=breakout, fee_bps=fee, slippage_bps=slip,
            expiry_reseed_limit=lifecycle['expiry_reseed_limit'] if reseed else 0,
            expiry_reseed_interval=lifecycle['expiry_reseed_interval'])
        account = account_identity(hashes, identity, scenario, arm, mode)
        directory = output / scenario / arm / mode
        started = perf_counter()
        run_experiment(directory, bars=bars, config=settings, rules=rules,
            capital=prereg['risk']['initial_capital_usdt'], fee_rate=D(fee)/10000,
            slippage=D(slip)/10000, participation=D(prereg['risk']['participation']),
            interval='1h', market=Market.BINANCE_USDM, instrument_id=identity['instrument_id'],
            data_identity=identity, no_trade=arm == 'cash', buy_hold=arm == 'fixed_budget_buy_hold',
            account_id=account, max_exit_quantity=D(prereg['risk']['max_order_base_units']),
            reference_budget=(dict(
                max_inventory=prereg['risk']['max_inventory_base_units'],
                max_order=prereg['risk']['max_order_base_units']) if arm == 'fixed_budget_buy_hold' else None))
        metrics = lifecycle_metrics(directory)
        fifo = attribution(directory)
        economics = fifo['summary']
        if abs(economics['reconciliation_residual']) > D('0.00000001'):
            raise ValueError(f'FIFO does not reconcile with authoritative NAV: {account}')
        if abs(economics['reported_equity_difference']) > D('0.00000001'):
            raise ValueError(f'reported NAV differs from authoritative NAV: {account}')
        nav = pd.read_csv(directory / 'daily.csv')
        trades = pd.read_csv(directory / 'trades.csv')
        if (metrics['max_inventory'] > float(prereg['risk']['max_inventory_base_units'])
                or trades.executed_shares.gt(float(prereg['risk']['max_order_base_units'])).any()
                or metrics['max_committed'] > prereg['risk']['max_committed_usdt']):
            raise ValueError(f'account exceeds frozen research budget: {account}')
        _write(directory / 'fifo.json', fifo)
        flat = {f'fifo_{section}_{key}': value
                for section in ('normal_sell', 'risk_exit', 'residual', 'completed_cycle')
                for key, value in economics[section].items()}
        rows.append(dict(scenario=scenario, arm=arm, mode=mode, account_id=account,
            wall_seconds=perf_counter()-started, **(metrics | dict(grid_cycles=economics['grid_cycles'])),
            **flat, fifo_reconciliation_residual=economics['reconciliation_residual'],
            initial_price_scale_exposure=float(nav.inventory.max()) * float(bars[0]['close'])))
        pd.DataFrame(rows).to_csv(output / 'summary.csv', index=False)
        print(f'{scenario}/{arm}/{mode}: equity={metrics["final_equity"]:.9f}, '
              f'buys={metrics["buy_fills"]}, sells={metrics["sell_fills"]}', flush=True)
    _write(output / 'study.json', dict(status='P7_R04B_MECHANISM_EVALUATED', runs=len(rows),
        wall_seconds=perf_counter()-wall, process_cpu_seconds=process_time()-cpu,
        output_bytes=sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
        exposure_status='EXPOSURE_NOT_MATCHED', funding='NOT_IN_PAPER_LEDGER',
        research_level='ASSUMPTION_BOUND_PAPER_RESEARCH', boundaries=BOUNDARIES))
    return pd.DataFrame(rows)


def preflight(output, *, snapshot, repository):
    """Record data/design admission without calculating economic results."""
    wall, cpu = perf_counter(), process_time()
    prereg, _, _, _, hashes = load_design(repository)
    _, identity = load_sample(snapshot)
    try:
        require_admission(repository)
        admission_status, reason = 'PASS', None
    except StudyBlocked as error:
        admission_status, reason = 'BLOCKED', str(error)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / 'preflight.json', dict(study='P7-R04B-E01', data_identity=identity,
        frozen_sha256=hashes, data_and_design='PASS', admission=admission_status,
        blocking_reason=reason, formal_accounts_executed=0,
        frozen_accounts=account_matrix(prereg),
        wall_seconds=perf_counter()-wall, process_cpu_seconds=process_time()-cpu,
        disk_available_bytes=shutil.disk_usage(output).free,
        boundaries=BOUNDARIES))
    return admission_status


def run_study(output, *, snapshot, repository):
    prereg, lifecycle, config, rules, hashes = load_design(repository)
    bars, identity = load_sample(snapshot)
    admission = require_admission(repository)
    output = Path(output)
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(parent).free < 100 * 1024 * 1024:
        raise StudyBlocked('less than the frozen 100 MiB research output budget available')
    result = execute_matrix(output, bars=bars, identity=identity, prereg=prereg,
        lifecycle=lifecycle, config=config, rules=rules, hashes=hashes)
    _write(output / 'admission.json', admission)
    return result
