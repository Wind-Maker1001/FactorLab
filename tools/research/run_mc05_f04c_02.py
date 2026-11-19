"""Run the preregistered, bounded real-market F04C-02 study. NO LIVE MONEY."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import perf_counter, process_time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from factorlab.dataset.partitioned import directory_bytes
from factorlab.errors import DataQualityError
from factorlab.experiments.mc_grid_tail_f04c_data import (
    ARCHIVE, MarketData, _bind_hourly_raw, _hourly_source, admit_market_data,
)
from factorlab.experiments.mc_grid_tail_f04c_windows import candidate_windows, screen_initial_orders
from factorlab.integrations.binance.market import milliseconds
from run_mc05_f04c import _encode, _paper_inputs, run


MAX_OBJECT = 2 * 1024 * 1024
MAX_DOWNLOAD = 8 * 1024 * 1024


def select_windows(hourly, manifest):
    indexed = {bar['time']: index for index, bar in enumerate(hourly)}
    candidates = []
    for candidate in candidate_windows():
        index = indexed[candidate['start']]
        hours = hourly[index - 1:index + 168]
        evidence = dict(instrument_id=manifest['contract']['instrument_id'], source=manifest['source'],
                        hourly_source=dict(data_version=manifest['data_version']))
        market_data = MarketData(tuple(hours), (), (), evidence)
        settings, _ = _paper_inputs(market_data)
        screening = screen_initial_orders(hours, settings) if candidate['id'].startswith('C') else None
        candidates.append(candidate | dict(anchor_time=hours[0]['time'], anchor_close=hours[0]['close'],
                                          screening=screening))
    selected_case = next((row['id'] for row in candidates[3:]
                          if row['screening']['first_potential_touch'] is not None), None)
    for candidate in candidates:
        candidate['selected'] = candidate['id'].startswith('I') or candidate['id'] == selected_case
    return candidates


def archive_objects(selected):
    objects = []
    for candidate in selected:
        start = datetime.fromisoformat(candidate['start'])
        for day in range(7):
            date = (start + timedelta(days=day)).date().isoformat()
            for interval in ('1m', '1d'):
                name = f'NVDAUSDT-{interval}-{date}.zip'
                url = f'{ARCHIVE}/daily/klines/NVDAUSDT/{interval}/{name}'
                for suffix in ('', '.CHECKSUM'):
                    objects.append(dict(name=name + suffix, url=url + suffix, window=candidate['id']))
    return objects


def acquire(root, objects):
    """Check exactly the selected objects; preserve receipts consumed by admission."""
    root.mkdir(parents=True, exist_ok=False)
    receipts, availability, rejected = [], [], {}
    started = perf_counter()
    total = 0
    for item in objects:
        try:
            with urlopen(Request(item['url'], method='HEAD'), timeout=20) as response:
                availability.append(dict(**item, status=response.status,
                    content_length=response.headers.get('Content-Length'),
                    checked_at=datetime.now(timezone.utc).isoformat()))
        except (HTTPError, URLError, TimeoutError) as error:
            rejected[item['window']] = f"archive unavailable: {item['url']}: {error}"
            availability.append(dict(**item, status='UNAVAILABLE', reason=str(error)))
    for item in objects:
        if item['window'] in rejected:
            continue
        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            with urlopen(item['url'], timeout=20) as response:
                content = response.read(MAX_OBJECT + 1)
                if len(content) > MAX_OBJECT or total + len(content) > MAX_DOWNLOAD:
                    rejected[item['window']] = 'preregistered archive byte budget exceeded'
                    break
                receipt = dict(**item, status=response.status, bytes=len(content),
                    sha256=sha256(content).hexdigest(), fetched_at=fetched_at,
                    last_modified=response.headers.get('Last-Modified'),
                    server_date=response.headers.get('Date'))
        except (HTTPError, URLError, TimeoutError) as error:
            rejected[item['window']] = f"archive acquisition failed: {item['url']}: {error}"
            continue
        (root / item['name']).write_bytes(content)
        total += len(content)
        receipts.append(receipt)
    (root / 'requests.json').write_bytes(_encode(receipts))
    completed_names = {row['name'] for row in receipts}
    for item in objects:
        if item['name'] not in completed_names and item['window'] not in rejected:
            rejected[item['window']] = 'acquisition stopped at the preregistered budget'
    return dict(head_requests=availability, get_requests=len(receipts), downloaded_bytes=total,
                rejected_windows=rejected, acquisition_wall_seconds=perf_counter() - started)


def finer_evidence(window_id, minute):
    """A single historical day, three official product probes, no trade download."""
    date = minute[:10]
    observations = []
    for product in ('aggTrades', 'bookTicker', 'bookDepth'):
        name = f'NVDAUSDT-{product}-{date}.zip'
        url = f'{ARCHIVE}/daily/{product}/NVDAUSDT/{name}'
        row = dict(window=window_id, product=product, date=date, url=url,
                   checked_at=datetime.now(timezone.utc).isoformat())
        try:
            with urlopen(Request(url, method='HEAD'), timeout=20) as response:
                row.update(status=response.status, content_length=response.headers.get('Content-Length'),
                           last_modified=response.headers.get('Last-Modified'))
        except HTTPError as error:
            row.update(status=error.code)
        except URLError as error:
            row.update(status='NETWORK_ERROR', error=str(error.reason))
        observations.append(row)
    return dict(probes=observations, trade_payload_downloaded=False,
                queue_position='NOT_AVAILABLE', historical_spread_depth='NOT_ADMITTED',
                minute_trade_fields='trade_count, taker_buy_volume, taker_buy_quote_volume')


def _difference(accounts):
    left, right = (accounts[arm]['metrics'] for arm in ('A', 'B'))
    return {key: Decimal(str(right[key])) - Decimal(str(left[key]))
            for key in left if key not in {'first_fill', 'terminal_open_orders'}}


def execute(args):
    from factorlab.experiments.mc_grid_tail_f04c_cases import extract_execution_cases
    from mc05_f04c_02_report import render_reports

    started, cpu_started = perf_counter(), process_time()
    root = Path(__file__).resolve().parents[2]
    protocol = root / 'docs/validation/mc-05-f04c-02/PROTOCOL.md'
    base = subprocess.check_output(['git', 'log', '-1', '--format=%H', '--',
                                    'docs/validation/mc-05-f04c-02/PROTOCOL.md'], cwd=root, text=True).strip()
    hourly, manifest, _, _ = _hourly_source(args.hourly_snapshot)
    candidates = select_windows(hourly, manifest)
    selected = [row for row in candidates if row['selected']]
    # Freeze selected identities before availability, minute data or account outcomes.
    args.output.mkdir(parents=True, exist_ok=args.resume)
    (args.output / 'DATA_COVERAGE.md').write_text(
        '# MC-05-F04C-02 预定窗口\n\n选择已完成，尚未采集分钟数据或运行账户。\n\n'
        + '\n'.join(f"- {row['id']} {row['start']} → {row['end_exclusive']}; selected={row['selected']}"
                    for row in candidates) + '\n', encoding='utf-8')
    # Screening itself uses hourly prices, so bind its entire existing snapshot too.
    _bind_hourly_raw(hourly, manifest, None)
    objects = archive_objects(selected)
    if args.acquire:
        acquisition = acquire(args.archive_root, objects)
    else:
        recorded = json.loads((args.archive_root / 'requests.json').read_text(encoding='utf-8'))
        acquisition = dict(mode='OFFLINE_REUSE', get_requests=0, downloaded_bytes=0,
            retained_objects=len(recorded), retained_bytes=sum(row['bytes'] for row in recorded),
            preregistered_head_request_limit=len(objects), retained_successful_get_receipts=len(recorded),
            original_failed_request_details='not retained before evidence interruption; missing objects are rejected',
            first_fetched_at=min(row['fetched_at'] for row in recorded),
            last_fetched_at=max(row['fetched_at'] for row in recorded),
            acquisition_timing='initial acquisition completed; per-object receipts retained; '
                               'original perf_counter interval was interrupted before final report')
        recorded_names = {row['name'] for row in recorded}
        acquisition['rejected_windows'] = {}
        for candidate in selected:
            missing = [item['name'] for item in objects
                       if item['window'] == candidate['id'] and item['name'] not in recorded_names]
            if missing:
                acquisition['rejected_windows'][candidate['id']] = (
                    'incomplete bounded acquisition receipts; no retry or replacement window: '
                    + ', '.join(missing))
    (args.output / 'AUDIT.json').write_bytes(_encode(dict(
        research='MC-05-F04C-02', stage='ARCHIVES_ACQUIRED', acquisition=acquisition)))

    windows = []
    touched = None
    for candidate in selected:
        if candidate['id'] in acquisition.get('rejected_windows', {}):
            windows.append(candidate | dict(status='MARKET_DATA_BLOCKED',
                rejection=acquisition['rejected_windows'][candidate['id']]))
            continue
        try:
            market_data = admit_market_data(args.hourly_snapshot, args.archive_root,
                                           candidate['start'], candidate['end_exclusive'])
        except DataQualityError as error:
            windows.append(candidate | dict(status='MARKET_DATA_BLOCKED', rejection=str(error)))
            continue
        directory = args.accounts_root / candidate['id']
        prefix = f"mc05-f04c-02-{candidate['id']}"
        if args.resume and directory.exists():
            result = json.loads((directory / 'AUDIT.json').read_text(encoding='utf-8'))
        else:
            result = run(directory, market_data, account_prefix=prefix)
        cases = extract_execution_cases(result['accounts'], market_data, Decimal('0.0006'))
        windows.append(candidate | dict(status='MARKET_DATA_ACCEPTED', data=result['data'],
            configuration=result['configuration'], accounts=result['accounts'],
            account_ids={arm: f'{prefix}-{arm}' for arm in ('A', 'B')},
            order_pairing=result['order_pairing'], execution_cases=cases,
            execution_cases_sha256=sha256(_encode(cases)).hexdigest(),
            paired_difference_b_minus_a=_difference(result['accounts']),
            result_sha256=result['result_sha256'], resources=result['resources']))
        # Repeat just once: requested deterministic comparison, same admitted inputs.
        repeated = args.accounts_root / 'repeat' / candidate['id']
        if args.resume and repeated.exists():
            replay = json.loads((repeated / 'AUDIT.json').read_text(encoding='utf-8'))
        else:
            replay = run(repeated, market_data, account_prefix=prefix)
        if result['result_sha256'] != replay['result_sha256']:
            raise ValueError(f"non-deterministic economic evidence: {candidate['id']}")
        windows[-1]['repeat_result_sha256'] = replay['result_sha256']
        windows[-1]['repeat_resources'] = replay['resources']
        if touched is None and (result['accounts']['A']['observations']['ohlc_order_minute_touches']
                                or result['accounts']['B']['metrics']['fills']):
            touched = (candidate['id'], min(touch['minute'] for arm in cases['arms'].values()
                                            for touch in arm['touches']))
        if process_time() - cpu_started > 600 or perf_counter() - started > 900:
            raise TimeoutError('preregistered research runtime budget exceeded')

    legacy_data = admit_market_data(args.hourly_snapshot, args.legacy_archive_root,
                                   '2026-08-05', '2026-08-12')
    legacy_output = args.accounts_root / 'frozen-regression'
    if args.resume and legacy_output.exists():
        legacy_actual = json.loads((legacy_output / 'AUDIT.json').read_text(encoding='utf-8'))
    else:
        legacy_actual = run(legacy_output, legacy_data)
    legacy_expected = json.loads((root / 'docs/validation/mc-05-f04c/AUDIT.json').read_text(encoding='utf-8'))
    if legacy_actual['result_sha256'] != legacy_expected['result_sha256']:
        raise ValueError('frozen F04C economic evidence changed')
    accepted = [row for row in windows if row['status'] == 'MARKET_DATA_ACCEPTED']
    independent = [row for row in accepted if row['sample'] == 'INDEPENDENT_HISTORICAL_WINDOW']
    counts = dict(predeclared_windows=3, accepted_windows=len(independent),
        touch_windows=sum(bool(row['accounts']['A']['observations']['ohlc_order_minute_touches']) for row in independent),
        paper_fill_windows=sum(any(row['accounts'][arm]['metrics']['fills'] for arm in ('A', 'B')) for row in independent),
        different_fill_windows=sum(bool(row['order_pairing']['order_trajectories_with_different_fills']) for row in independent),
        different_nav_windows=sum(row['accounts']['A']['metrics']['terminal_nav']
                                  != row['accounts']['B']['metrics']['terminal_nav'] for row in independent))
    nonzero = any(row['accounts'][arm]['metrics']['fills'] for row in accepted for arm in ('A', 'B'))
    different = any(row['order_pairing']['order_trajectories_with_different_fills'] for row in accepted)
    states = ['ENGINEERING_ACCEPTED',
        'MARKET_DATA_ACCEPTED' if len(accepted) == len(selected) else 'MARKET_DATA_BLOCKED',
        'WINDOW_SELECTION_PRE_REGISTERED',
        'PAPER_NONZERO_FILL_OBSERVED' if nonzero else 'NO_FILL_CASE_OBSERVED',
        'EXECUTION_GRANULARITY_DIFFERENCE_OBSERVED' if different else 'INCONCLUSIVE',
        'FIFO_NAV_RECONCILED', 'REAL_FILL_NOT_VERIFIED', 'FINANCIAL_NOT_ESTABLISHED', 'NO LIVE MONEY']
    identity = dict(research='MC-05-F04C-02', protocol_commit=base,
        baseline_commit='053345e08e4c20d9329b3ea6fe1afa308cb00247',
        protocol_sha256=sha256(protocol.read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
        candidates=candidates, windows=windows, independent_counts=counts,
        frozen_regression=dict(expected=legacy_expected['result_sha256'], actual=legacy_actual['result_sha256']),
        states=states)
    # Acquisition times and measured runtimes do not define economic reproducibility.
    stable = dict(protocol_sha256=identity['protocol_sha256'], candidates=candidates,
                  windows=[dict(id=row['id'], result_sha256=row['result_sha256'],
                                execution_cases_sha256=row['execution_cases_sha256']) for row in accepted])
    audit = identity | dict(result_sha256=sha256(_encode(stable)).hexdigest(),
        acquisition=acquisition, finer_evidence=finer_evidence(*touched) if touched else None,
        resources=dict(wall_seconds=perf_counter() - started, cpu_seconds=process_time() - cpu_started,
            account_bytes=directory_bytes(args.accounts_root), formal_accounts=len(accepted) * 2,
            repeat_accounts=len(accepted) * 2, frozen_regression_accounts=2,
            formal_replay_cpu_seconds=sum(row['resources']['replay_and_audit_cpu_seconds'] for row in accepted),
            repeat_replay_cpu_seconds=sum(row['repeat_resources']['replay_and_audit_cpu_seconds'] for row in accepted)),
        validation=dict(deterministic_replay='PASS', frozen_f04c='PASS',
                        accounting_tolerance='0.00000001', targeted_tests='PENDING'))
    (args.output / 'AUDIT.json').write_bytes(_encode(audit))
    render_reports(args.output, audit)
    print(json.dumps(dict(states=states, independent_counts=counts,
                          result_sha256=audit['result_sha256'], resources=audit['resources'])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hourly-snapshot', type=Path, required=True)
    parser.add_argument('--archive-root', type=Path, required=True)
    parser.add_argument('--legacy-archive-root', type=Path, required=True)
    parser.add_argument('--accounts-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--resume', action='store_true', help='reuse completed accounts after an interrupted evidence pass')
    execute(parser.parse_args())


if __name__ == '__main__':
    main()
