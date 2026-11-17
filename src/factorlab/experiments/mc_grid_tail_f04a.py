"""Repeat the first frozen F03 price paths with three Paper order lifetimes."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from functools import partial
from hashlib import sha256
from io import BytesIO
import json
from multiprocessing import get_context
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from time import perf_counter

import pandas as pd

from factorlab._identity import source_context_root
from factorlab.dataset.partitioned import directory_bytes
from factorlab.experiments.grid_monte_carlo import historical_input
from factorlab.experiments.grid_paper import D
from factorlab.experiments.mc_grid_tail_convergence import _account_results, account_metrics, batches, price_digest, write
from factorlab.experiments.mc_grid_tail_f03_resources import ResourceMonitor
from factorlab.experiments.mc_grid_tail_f04a_diagnostics import capture_ttl_account
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.regime_jump import PathPlan
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules

PATH_KEYS = ('model', 'seed', 'path_id')
RESIDUALS = ('reconciliation_residual', 'nav_residual', 'inventory_residual')
F03_NON_ECONOMIC = {
    *PATH_KEYS, 'temporary_account_bytes', 'price_minimum', 'worst_hour_return', 'worst_24h_return',
    'return_autocorrelation_1', 'squared_return_autocorrelation_1', 'hourly_return_volatility',
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def study_definition(design, root):
    f03 = read_json(root/design['frozen_definition'])
    f02 = read_json(root/f03['frozen_definition'])
    definition = {key: f02[key] for key in (
        'dataset', 'as_of', 'models', 'model_parameters', 'model_versions', 'bootstrap',
        'grid', 'rules', 'capacity', 'costs', 'start', 'threshold_as_of',
    )}
    return definition | design | {
        'experiment': design['experiment'] | {key: f03['experiment'][key]
            for key in ('seeds', 'steps', 'batch_size')},
        'frozen_f03_definition': design['frozen_definition'],
        'frozen_execution_account_scratch_bytes': f02['resource_planning']['expected_temporary_disk_bytes'],
    }


def identity_tuple(identity):
    return tuple(identity[key] for key in PATH_KEYS)


def _account_id(identity, ttl):
    model, seed, path_id = identity_tuple(identity)
    return f'mc05-f04a-{model}-{seed}-{path_id}-ttl-{ttl}'


def validate_triplet(rows, path_identity, price_hash, ttls):
    expected = [(ttl, _account_id(path_identity, ttl)) for ttl in ttls]
    actual = [(row['ttl_hours'], row['account_id']) for row in rows]
    if actual != expected or any(
        identity_tuple(row) != identity_tuple(path_identity) or row['price_sha256'] != price_hash
        for row in rows
    ):
        raise ValueError(f'incomplete or mismatched TTL triplet: {path_identity}')


def _frozen_accounts(path):
    content = path.read_bytes().replace(b'\r\n', b'\n')
    frame = pd.read_csv(BytesIO(content), float_precision='round_trip')
    indexed = frame.set_index(list(PATH_KEYS), verify_integrity=True)
    return indexed.drop(columns=[key for key in F03_NON_ECONOMIC if key in indexed]), sha256(content).hexdigest()


def _paths(inputs, receipt, definition, baseline, *, pilot):
    settings = definition['experiment']
    params = definition['model_parameters']
    generators, rejection = scenario_models(inputs, receipt, threshold_as_of=definition['threshold_as_of'])
    if any(model not in generators or generators[model].receipt()['parameters'] != fixed
           for model, fixed in params.items()):
        raise ValueError(f'F03 model calibration identity changed: {rejection}')
    count = definition['resources']['pilot_paths_per_model'] if pilot else settings['paths_per_model_seed']
    seeds = settings['seeds'][:1] if pilot else settings['seeds']
    for model in definition['models']:
        generator = generators.get(model)
        for seed in seeds:
            plan = PathPlan(seed=seed, n_paths=settings['paths_per_model_seed'], n_steps=settings['steps'],
                            batch_size=settings['batch_size'])
            paths = []
            for begin, batch, _ in batches(
                model, inputs, generator, seed=seed, begin=0, end=plan.n_paths,
                batch_size=plan.batch_size, steps=plan.n_steps, block_size=definition['bootstrap']['block_size'],
            ):
                for i, prices in enumerate(batch, begin):
                    if i == count:
                        break
                    identity = dict(model=model, seed=seed, path_id=i)
                    digest = price_digest(prices)
                    if baseline.at[identity_tuple(identity), 'price_sha256'] != digest:
                        raise ValueError(f'price path differs from frozen F03: {identity}')
                    latent = generator.path_evidence(plan, i) if generator is not None else None
                    paths.append((identity, prices, latent, digest))
                if len(paths) == count:
                    break
            if len(paths) != count:
                raise ValueError(f'frozen price path prefix is incomplete: {model}/{seed}')
            yield model, seed, paths


def _replay(prices, payload, *, definition, temporary, rules, configs, provenance):
    identity, ttl, latent = payload
    account_id = _account_id(identity, ttl)
    config = configs[ttl]
    with TemporaryDirectory(dir=temporary, prefix='account-') as directory:
        account = Path(directory)/'paper'
        diagnostic = partial(capture_ttl_account, account=account, definition=definition,
                             config=asdict(config), rules=rules)
        row = account_metrics(
            prices, start=definition['start'], config=config, rules=rules, capacity=definition['capacity'],
            output=account, provenance=provenance, diagnostic=diagnostic, latent=deepcopy(latent),
            accounting_evidence=True, account_id=account_id,
        )
        if any(row[key] != 0 for key in RESIDUALS):
            raise ValueError(f'nonzero FIFO/NAV/inventory residual in {account_id}')
        return identity | dict(ttl_hours=ttl, account_id=account_id, price_sha256=price_digest(prices)) | row


def _compare_baseline(row, baseline):
    old = baseline.loc[identity_tuple(row)]
    differences = [key for key, value in old.items()
                   if not (row[key] is None and pd.isna(value) or row[key] == value)]
    if differences:
        raise ValueError(f'F03 TTL=100 economic mismatch in {row["account_id"]}: {differences}')


def _economic_digest(row):
    payload = {key: value for key, value in row.items() if key != 'temporary_account_bytes'}
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _collect(executor, paths, ttls, replay, workers, deadline, policy, performance):
    jobs = ((identity, prices, (identity, ttl, latent))
            for identity, prices, latent, _ in paths for ttl in ttls)
    with closing(_account_results(executor, jobs, replay, workers)) as accounts:
        for identity, _, _, digest in paths:
            triplet = []
            for _ in ttls:
                _, _, row = next(accounts)
                triplet.append(row)
                _enforce_active_limit(deadline, policy, performance)
            validate_triplet(triplet, identity, digest, ttls)
            yield triplet


def _enforce_active_limit(deadline, policy, performance):
    if perf_counter() > deadline:
        raise TimeoutError('F04A replay exceeded the active wall budget')
    if performance['peak_tree_rss_bytes'] > policy['maximum_tree_rss_bytes']:
        raise MemoryError('F04A sampled process-tree RSS exceeded the cap')
    if performance['peak_temporary_disk_bytes'] > policy['maximum_temporary_bytes']:
        raise OSError('F04A sampled temporary disk usage exceeded the cap')


def admission(policy, estimate, *, available_seconds, available_memory_bytes, free_bytes):
    scratch = estimate['temporary_bytes']
    evidence = estimate['evidence_bytes']
    memory = estimate['tree_rss_bytes']
    reasons = []
    if estimate['wall_seconds'] + policy['output_reserve_seconds'] > available_seconds:
        reasons.append('WALL_BUDGET')
    if memory > min(available_memory_bytes, policy['maximum_tree_rss_bytes']):
        reasons.append('TREE_RSS')
    if scratch > policy['maximum_temporary_bytes']:
        reasons.append('TEMPORARY_CAP')
    if evidence > policy['maximum_evidence_bytes']:
        reasons.append('EVIDENCE_CAP')
    if scratch + evidence + policy['minimum_free_after_bytes'] > free_bytes:
        reasons.append('DISK_FREE')
    return dict(status='RESOURCE_NOT_ADMITTED' if reasons else 'RESOURCE_ADMITTED', reasons=reasons,
                estimate=estimate, available_seconds=available_seconds,
                available_memory_bytes=available_memory_bytes, free_bytes=free_bytes)


def _estimate(calibration, policy, path_groups, *, minimum_scratch):
    safety = policy['cost_safety_factor']
    performance = calibration['performance']
    model_pilot = {group['model']: group for group in performance['groups'] if group['model'] != 'serial'}
    planned_accounts = sum(len(paths) for _, _, paths in path_groups)*calibration['ttl_count']
    wall = sum(len(paths) / policy['pilot_paths_per_model'] * model_pilot[model]['wall_seconds']
               for model, _, paths in path_groups)
    cpu = sum(len(paths) / policy['pilot_paths_per_model'] * model_pilot[model]['tree_cpu_seconds']
              for model, _, paths in path_groups)
    return dict(wall_seconds=wall*safety, tree_cpu_seconds=cpu*safety,
                tree_rss_bytes=int(performance['peak_tree_rss_bytes']*safety),
                temporary_bytes=max(minimum_scratch, int(performance['peak_temporary_disk_bytes']*safety)),
                evidence_bytes=int(calibration['evidence_bytes_per_account']*planned_accounts*safety) + 262144)


def _csv_bytes(rows):
    return pd.DataFrame(rows).to_csv(index=False, lineterminator='\n').encode('utf-8')


def run(output, *, snapshot, design_path, calibration_path=None):
    """Calibrate on a fixed prefix or run the whole predeclared paired sample."""
    root = source_context_root()
    design = read_json(design_path)
    definition = study_definition(design, root)
    settings, policy = definition['experiment'], definition['resources']
    ttls, workers = settings['order_ttl_hours'], policy['max_workers']
    baseline_ttl = settings['baseline_ttl_hours']
    config = GridConfig(**definition['grid'])
    if config.order_ttl != baseline_ttl:
        raise ValueError('F04A baseline must match the F03 order TTL')
    configs = {ttl: replace(config, order_ttl=ttl) for ttl in ttls}
    rules = GridRules(**{key: D(value) for key, value in definition['rules'].items()})
    inputs, receipt = historical_input(snapshot, definition['as_of'], config.initial_price)
    if receipt != definition['dataset']:
        raise ValueError('F04A requires the complete F03 frozen data calibration identity')
    baseline, frozen_digest = _frozen_accounts(root/definition['frozen_accounts'])
    provenance = capture_provenance(root)
    pilot = calibration_path is None
    calibration = None if pilot else read_json(calibration_path)
    binding = dict(definition=definition, f03_accounts_normalized_csv_sha256=frozen_digest,
                   environment=provenance['environment'])
    if not pilot and (calibration['binding'] != binding or calibration['status'] != 'PRECALIBRATION_COMPLETE'):
        raise ValueError('F04A pilot does not match the study, frozen source or runtime')

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    monitor = ResourceMonitor()
    planned_paths = len(definition['models']) * (policy['pilot_paths_per_model'] if pilot else
                    len(settings['seeds']) * settings['paths_per_model_seed'])
    audit = dict(research_id=definition['research_id'], phase='pilot' if pilot else 'formal',
                 workers=workers, planned_paths=planned_paths, planned_accounts=planned_paths*len(ttls),
                 f03_accounts_reused=0, f03_path_and_economic_matches=0,
                 financial_status='FINANCIAL_NOT_ESTABLISHED', execution_authorization='NO LIVE MONEY',
                 engineering_status='ENGINEERING_BLOCKED', paired_sample_status='PAIRED_SAMPLE_INVALID',
                 ttl_sensitivity_status='TTL_SENSITIVITY_INCONCLUSIVE',
                 convergence_status='STATISTICAL_CONVERGENCE_NOT_ESTABLISHED',
                 binding=binding, execution_provenance=provenance)
    rows = []
    paths_by_group = []
    performance = {}
    try:
        with monitor.measure() as performance:
            budget = policy['pilot_wall_seconds' if pilot else 'formal_wall_seconds']
            deadline = perf_counter() + budget - policy['output_reserve_seconds']
            paths_by_group = list(_paths(inputs, receipt, definition, baseline, pilot=pilot))
            path_digests = {digest for _, _, paths in paths_by_group for _, _, _, digest in paths}
            if len(path_digests) != planned_paths:
                raise ValueError('F04A planned distinct prices do not match the frozen sample size')
            scratch_floor = workers*definition['frozen_execution_account_scratch_bytes']
            if pilot:
                estimate = dict(wall_seconds=0, tree_rss_bytes=policy['maximum_tree_rss_bytes'],
                                temporary_bytes=scratch_floor, evidence_bytes=262144)
            else:
                estimate = _estimate(calibration, policy, paths_by_group, minimum_scratch=scratch_floor)
            audit['admission'] = admission(
                policy, estimate, available_seconds=deadline-perf_counter()+policy['output_reserve_seconds'],
                available_memory_bytes=monitor.psutil.virtual_memory().available,
                free_bytes=shutil.disk_usage(output).free,
            )
            if audit['admission']['reasons']:
                raise OSError(f'F04A not admitted: {audit["admission"]["reasons"]}')
            with TemporaryDirectory(dir=output.parent, prefix='factorlab-mc05-f04a-') as temporary:
                replay = partial(_replay, definition=definition, temporary=temporary, rules=rules,
                                 configs=configs, provenance=provenance)
                with ProcessPoolExecutor(max_workers=workers, mp_context=get_context('spawn')) as executor:
                    for model, _, paths in paths_by_group:
                        with monitor.observe(model, len(paths), temporary, output):
                            for triplet in _collect(executor, paths, ttls, replay, workers, deadline, policy, performance):
                                baseline_row = next(row for row in triplet if row['ttl_hours'] == baseline_ttl)
                                _compare_baseline(baseline_row, baseline)
                                audit['f03_path_and_economic_matches'] += 1
                                rows.extend(triplet)
                if pilot:
                    serial = {}
                    with monitor.observe('serial', len(definition['models']), temporary, output):
                        for _, _, paths in paths_by_group:
                            for triplet in _collect(None, paths[:1], ttls, replay, 1, deadline, policy, performance):
                                serial.update((row['account_id'], _economic_digest(row)) for row in triplet)
                else:
                    serial = calibration['serial_economic_digests']
            _enforce_active_limit(deadline, policy, performance)

            matches = [row['account_id'] for row in rows if row['account_id'] in serial
                       and serial[row['account_id']] == _economic_digest(row)]
            if set(matches) != set(serial) or len(matches) != len(serial):
                raise ValueError('F04A serial and parallel economic results differ')
            audit['serial_parallel_exact_accounts'] = matches
            audit['baseline_compared_fields'] = list(baseline.columns)
            audit['maximum_absolute_residual'] = {key: max(abs(row[key]) for row in rows) for key in RESIDUALS}
            data = _csv_bytes(rows)
            if pilot:
                calibration = dict(status='PRECALIBRATION_COMPLETE', binding=binding, ttl_count=len(ttls),
                                   accounts=len(rows), serial_accounts=len(serial),
                                   evidence_bytes_per_account=len(data)/len(rows),
                                   serial_economic_digests=serial, performance=performance)
            else:
                from factorlab.experiments.mc_grid_tail_f04a_report import write_reports

                if len(data) > policy['maximum_evidence_bytes']:
                    raise OSError('F04A compact account evidence exceeds the evidence cap')
                results = write_reports(output, rows)
                (output/'paths.csv').write_bytes(data)
                audit.update(results, account_csv_normalized_lf_sha256=sha256(data).hexdigest(),
                             paired_sample_status='PAIRED_SAMPLE_VALID')
            if directory_bytes(output) > policy['maximum_evidence_bytes']:
                raise OSError('F04A study evidence exceeds the evidence cap')
            audit['engineering_status'] = 'ENGINEERING_ACCEPTED'
        if pilot:
            write(output/'calibration.json', calibration, atomic=True)
    except BaseException as error:
        audit.update(engineering_status='ENGINEERING_BLOCKED', paired_sample_status='PAIRED_SAMPLE_INVALID',
                     ttl_sensitivity_status='TTL_SENSITIVITY_INCONCLUSIVE',
                     failure=dict(type=type(error).__name__, message=str(error)))
        raise
    finally:
        completed = {identity_tuple(row) for row in rows}
        audit.update(complete_paths=len(completed), collected_accounts=len(rows), resources=performance,
                     missing_pairs=[identity for _, _, paths in paths_by_group for identity, _, _, _ in paths
                                    if identity_tuple(identity) not in completed],
                     evidence_bytes_before_audit=directory_bytes(output))
        write(output/'AUDIT.json', audit, atomic=True)
    return audit
