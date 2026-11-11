"""Read-only, ex-post execution input diagnostics; never a selection interface."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
import zipfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.data.evidence import json_bytes
from factorlab.data.market_evidence_sidecar import ALLOWED, SAFETY, load_sidecar
from factorlab.dataset.identity import json_hash

KEYS = ['date', 'symbol']
PRICES = ['raw_open', 'raw_high', 'raw_low', 'raw_close']
RISK_COLUMNS = KEYS + ['issue_type', 'source', 'source_field', 'raw_value',
    'research_impact', 'evidence_available', 'sidecar_coverage', 'recommended_action',
    'verification_status', 'knowledge_class', 'fold', 'member', 'holding_exposure']


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                              default=str) + '\n', encoding='utf-8')


def checked(raw, expected):
    if digest(raw) != expected:
        raise ValueError('INPUT_HASH_DRIFT')
    return raw


def validate_symbols(symbols):
    if len(symbols) != len(set(symbols)) or any(
            re.fullmatch(r'(?:[03]\d{5}\.SZ|6\d{5}\.SH)', s) is None for s in symbols):
        raise ValueError('ILLEGAL_SECURITY_IDENTITY')


def frozen_inputs(config):
    loaded = {}
    for name, pin in config['inputs'].items():
        raw = (ROOT / pin['path']).read_bytes().replace(b'\r\n', b'\n')
        loaded[name] = json.loads(checked(raw, pin['lf_sha256']))
    manifest, freeze = loaded['manifest'], loaded['freeze']
    if json_hash(manifest['entries']) != manifest['identity'] or manifest['identity'] != freeze['data_identity']:
        raise ValueError('FROZEN_DATA_IDENTITY_DRIFT')
    validate_symbols(manifest['symbols'])
    if json_hash(loaded['study']) != freeze['config_hash']:
        raise ValueError('FROZEN_STUDY_CONFIG_DRIFT')
    return loaded


def read_table(archive, category, table, columns, lo, hi, symbols, pins, entries, documents):
    """Decompress each required shard once; predicate-filter before pandas materialization."""
    parts = []
    prefix = category + '/' + table + '/'
    for name in sorted(archive.namelist()):
        if not name.startswith(prefix) or not name.endswith('.parquet'):
            continue
        start, end = Path(name).stem.split('_')
        if pd.Timestamp(end) < lo or pd.Timestamp(start) > hi:
            continue
        raw = archive.read(name)
        sha = digest(raw)
        receipt = json.loads(archive.read(name + '.receipt.json'))
        if sha != pins[(category + '.zip', name)] or sha != receipt['sha256']:
            raise ValueError('INPUT_HASH_DRIFT: ' + name)
        filters = [('date', '>=', lo), ('date', '<=', hi)]
        if symbols is not None:
            filters.append(('instrument', 'in', symbols))
        frame = pd.read_parquet(io.BytesIO(raw), columns=columns, filters=filters)
        if symbols is None:
            frame = frame.loc[frame.market_code.eq('CN')]
        frame = frame.rename(columns={'instrument': 'symbol'})
        frame['source_sha256'] = sha
        frame['source_member'] = name
        parts.append(frame)
        if sha in documents:
            documents[sha] = raw
        entries.append(dict(archive=category + '.zip', member=name, sha256=sha,
            selected_rows=len(frame), columns=columns, uncompressed_bytes=len(raw)))
    return pd.concat(parts, ignore_index=True)


def load_market(args, loaded):
    manifest, study = loaded['manifest'], loaded['study']
    pins = {(e['archive'], e['member']): e['actual_sha256'] for e in manifest['entries']}
    symbols = manifest['symbols']
    with zipfile.ZipFile(args.membership_zip) as archive:
        raw = checked(archive.read('members_daily.csv'), pins[('csi500_full_history.zip', 'members_daily.csv')])
        checked(archive.read('manifest.json'), pins[('csi500_full_history.zip', 'manifest.json')])
        members = pd.read_csv(io.BytesIO(raw), usecols=['date', 'stock_code'])
    members['date'] = pd.to_datetime(members.date)
    members['symbol'] = members.stock_code.str.replace('.XSHG', '.SH', regex=False).str.replace('.XSHE', '.SZ', regex=False)
    initial = members.loc[members.date.eq(study['start']), 'symbol'].unique()
    rebuilt = sorted(initial, key=lambda s: digest(s.encode()))[:study['symbol_limit']]
    if rebuilt != symbols:
        raise ValueError('FIRST_DAY_COHORT_DRIFT')
    members = members.loc[members.symbol.isin(symbols), KEYS]
    if members.duplicated(KEYS).any():
        raise ValueError('DUPLICATE_MEMBERSHIP_KEY')
    sidecar = loaded['sidecar']
    documents = {r['source_document_sha256']: None for r in sidecar['candidates']}
    documents[loaded['sources']['observation_identity']['source_sha256']] = None
    entries = []
    with zipfile.ZipFile(args.bigquant_root / 'common.zip') as archive:
        cal = read_table(archive, 'common', 'trading_days', ['date', 'market_code'],
            pd.Timestamp(study['warm_start']), pd.Timestamp(study['tail_end']), None, pins, entries, documents)
    if cal.date.duplicated().any():
        raise ValueError('DUPLICATE_CALENDAR_KEY')
    calendar = pd.DatetimeIndex(cal.date.sort_values())
    fold_dates = {}
    for fold in loaded['freeze']['folds']:
        lo, hi = map(pd.Timestamp, fold['test'])
        for day in calendar[(calendar >= lo) & (calendar < hi)]:
            if day in fold_dates:
                raise ValueError('OVERLAPPING_TEST_FOLDS')
            fold_dates[day] = fold['fold']
    test = pd.DatetimeIndex(sorted(fold_dates))
    if len(test) != 726 or len(fold_dates) == 0 or len(symbols) != 128 or len(loaded['freeze']['folds']) != 3:
        raise ValueError('FROZEN_SCOPE_DRIFT')
    # 61 trailing signal sessions and a preceding observation; six tail sessions
    # bound next-open execution and the frozen label horizon without trading it.
    left = calendar.get_loc(test[0]) - 62
    right = calendar.get_loc(test[-1]) + 6
    scan = calendar[left:right + 1]
    if not members.loc[members.date.between(scan[0], scan[-1]), 'date'].isin(calendar).all():
        raise ValueError('MEMBER_DATE_MISSING_FROM_CALENDAR')
    prices = ['date', 'instrument', 'open', 'high', 'low', 'close', 'volume', 'pre_close', 'upper_limit', 'lower_limit', 'adjust_factor']
    states = ['date', 'instrument', 'suspended', 'st_status', 'is_risk_warning', 'price_limit_status', 'exdr']
    with zipfile.ZipFile(args.bigquant_root / 'stock.zip') as archive:
        bars = read_table(archive, 'stock', 'cn_stock_real_bar1d', prices, scan[0], scan[-1], symbols, pins, entries, documents)
        status = read_table(archive, 'stock', 'cn_stock_status', states, scan[0], scan[-1], symbols, pins, entries, documents)
    bars = bars.rename(columns={c: 'raw_' + c for c in ('open', 'high', 'low', 'close')})
    return bars, status, members, scan, test, fold_dates, entries, documents


def evidence_index(args, loaded, bars, status, documents):
    sources = loaded['sources']
    for item in sources['documents']:
        documents[item['sha256']] = checked((args.source_root / item['local_name']).read_bytes(), item['sha256'])
    observation = sources['observation_identity']
    row = bars.loc[bars.symbol.eq(observation['security']) & bars.date.eq(observation['date'])]
    if (len(row) != 1 or str(row.raw_close.iloc[0]) != observation['price']
            or int(row.volume.iloc[0]) != observation['volume']
            or row.source_sha256.iloc[0] != observation['source_sha256']):
        raise ValueError('SIDECAR_PREVIOUS_OBSERVATION_DRIFT')
    documents[digest(json_bytes(observation))] = json_bytes(observation)
    originals = {}
    for symbol, day, field in ALLOWED:
        local = bars.loc[bars.symbol.eq(symbol) & bars.date.eq(day)]
        state = status.loc[status.symbol.eq(symbol) & status.date.eq(day)]
        if len(local) != 1 or len(state) != 1:
            raise ValueError('SIDECAR_SOURCE_KEY_CONFLICT')
        if field in ('valuation_mark', 'suspension_confirmed'):
            value = local.raw_close.iloc[0]
            if state.suspended.iloc[0] != 1 or local.volume.iloc[0] != 0:
                raise ValueError('SIDECAR_SUSPENSION_CONFLICT')
        elif field == 'is_st':
            for key, expected in loaded['sidecar']['original_supplier_state'].items():
                if key != 'adapter_is_st' and state[key].iloc[0] != expected:
                    raise ValueError('SIDECAR_STATUS_DRIFT')
            value = state.st_status.where(state.st_status.isin([0, 1])).iloc[0]
        else:
            # The original adapter had no evidence field of either name.
            value = None
        originals[(symbol, day, field)] = None if value is None or pd.isna(value) else value.item()
    raw_manifest = args.manifest.read_bytes()
    if json.loads(raw_manifest) != loaded['manifest']:
        raise ValueError('PRIVATE_MANIFEST_IDENTITY_DRIFT')
    evidence = load_sidecar(ROOT / loaded['_config']['inputs']['sidecar']['path'],
        source_manifest=raw_manifest, originals=originals, documents=documents)
    return evidence


def make_panel(bars, status, members, scan, symbols):
    validate_symbols(symbols)
    grid = pd.MultiIndex.from_product([scan, symbols], names=KEYS).to_frame(index=False)
    duplicates = []
    for source, frame in [('PRICE', bars), ('STATE', status)]:
        duplicates.extend(dict(date=r['date'], symbol=r['symbol'], source=source, count=int(r['count']))
            for r in frame.groupby(KEYS).size().rename('count').reset_index().query('count > 1').to_dict('records'))
    # Ambiguous rows are diagnosed, not silently resolved to a chosen vendor row.
    price = bars.loc[~bars.duplicated(KEYS, keep=False)].rename(columns={'source_sha256': 'price_sha', 'source_member': 'price_member'})
    state = status.loc[~status.duplicated(KEYS, keep=False)].rename(columns={'source_sha256': 'state_sha', 'source_member': 'state_member'})
    panel = grid.merge(price.assign(price_key=True), on=KEYS, how='left', validate='one_to_one')
    panel = panel.merge(state.assign(state_key=True), on=KEYS, how='left', validate='one_to_one')
    panel = panel.merge(members.assign(member=True), on=KEYS, how='left', validate='one_to_one')
    panel['member'] = panel.member.eq(True)
    panel = panel.sort_values(['symbol', 'date']).reset_index(drop=True)
    numeric = panel[PRICES + ['volume']].to_numpy(dtype=float)
    finite = np.isfinite(numeric)
    ohlc = (panel.raw_low.le(panel[['raw_open', 'raw_close']].min(axis=1))
        & panel.raw_high.ge(panel[['raw_open', 'raw_close']].max(axis=1))
        & panel.raw_low.le(panel.raw_high) & panel[PRICES].gt(0).all(axis=1))
    panel['valid_ohlc'] = finite[:, :4].all(axis=1) & ohlc
    panel['observed_close'] = (np.isfinite(panel.raw_close) & panel.raw_close.gt(0)
        & panel.valid_ohlc & panel.volume.gt(0) & np.isfinite(panel.volume) & ~panel.suspended.eq(1))
    panel['observed_valid'] = panel.valid_ohlc & finite.all(axis=1) & panel.volume.gt(0) & panel.suspended.eq(0)
    panel['suspension_proxy'] = panel.suspended.eq(1) | (panel.volume.eq(0) & ~panel.observed_close)
    panel['previous_close'] = panel.raw_close.where(panel.observed_close).groupby(panel.symbol).transform(lambda s: s.ffill().shift())
    panel['previous_date'] = panel.date.where(panel.observed_close).groupby(panel.symbol).transform(lambda s: s.ffill().shift())
    panel['next_date'] = panel.date.where(panel.observed_close).groupby(panel.symbol).transform(lambda s: s.bfill().shift(-1))
    panel['next_close'] = panel.raw_close.where(panel.observed_close).groupby(panel.symbol).transform(lambda s: s.bfill().shift(-1))
    return panel, duplicates


def diagnose(panel, test, folds, duplicates, evidence=None):
    local = panel.loc[panel.date.isin(test)].copy()
    local['fold'] = local.date.map(folds)
    facts = {} if evidence is None else {(f.security, f.day, f.field): f for f in evidence.facts}
    coverage = []
    for (symbol, day, field), fact in sorted(facts.items()):
        coverage.append(dict(date=day, symbol=symbol, field=field,
            evidence_class=fact.evidence_class, verification_status=fact.verification_status,
            knowledge_class=fact.known_at_status, coverage='EXACT_VALIDATED_CANDIDATE',
            trading_permission=False, policy_approved_for_frozen_p9_01=False))
    risks = []
    def add(mask, issue, field, impact, action, verification='SOURCE_OBSERVED', source='BIGQUANT'):
        for row in local.loc[mask].itertuples():
            fields = [f for s, d, f in facts if s == row.symbol and d == str(row.date.date())]
            value = getattr(row, field, None)
            risks.append(dict(date=str(row.date.date()), symbol=row.symbol, issue_type=issue,
                source=source, source_field=field,
                raw_value='null' if value is None or pd.isna(value) else str(value),
                research_impact=impact, evidence_available=';'.join(fields) or 'NONE',
                sidecar_coverage='EXACT_VALIDATED_CANDIDATE' if fields else 'NONE',
                recommended_action=action, verification_status=verification,
                knowledge_class='EX_POST_DATA_DIAGNOSTIC', fold=int(row.fold), member=bool(row.member),
                holding_exposure='HOLDING_EXPOSURE_NOT_ASSESSED'))
    add(~local.price_key.eq(True), 'MISSING_PRICE', 'price_key', 'MISSING_BAR', 'Locate source bar; never synthesize an execution price')
    for field, impact in [('raw_open', 'MISSING_OPEN'), ('raw_close', 'MISSING_CLOSE')]:
        add(local.price_key.eq(True) & local[field].isna(), 'MISSING_PRICE', field, impact, 'Separate missing observation from a suspension valuation assumption')
    values = local[PRICES + ['volume']].to_numpy(dtype=float)
    for field in PRICES + ['volume']:
        add(local.price_key.eq(True) & ~np.isfinite(local[field]), 'MISSING_PRICE', field,
            'NONFINITE_VALUE', 'Inspect field coverage; missing and zero are distinct')
    add(local.price_key.eq(True) & np.isfinite(values[:, :4]).all(axis=1) & ~local.valid_ohlc,
        'MISSING_PRICE', 'raw_close', 'INVALID_OHLC', 'Verify raw-price semantics without automatic correction')
    add(local.volume.lt(0), 'MISSING_PRICE', 'volume', 'INVALID_VOLUME', 'Verify source volume')
    add(local.suspended.eq(1), 'KNOWN_NONTRADABLE', 'suspended', 'SUPPLIER_SUSPENSION_NOT_EXECUTION_PRICE', 'Retain nontradable status; confirm evidence for valuation')
    for field in ['suspended', 'st_status']:
        add(local[field].isna(), 'MISSING_STATE', field, 'SOURCE_STATE_MISSING', 'Obtain state evidence; do not default to normal')
        add(local[field].notna() & ~local[field].isin([0, 1]), 'UNSUPPORTED_STATUS_ENUM', field,
            'SOURCE_PRESENT_ADAPTER_DISCARDS_SEMANTICS_UNVERIFIED', 'Keep raw enum; require versioned semantics or exact sidecar')
    add(local.is_risk_warning.notna() & ~local.is_risk_warning.isin([0, 1]),
        'UNSUPPORTED_STATUS_ENUM', 'is_risk_warning', 'READABLE_ECONOMIC_SEMANTICS_UNKNOWN', 'Verify vendor dictionary')
    add(local.is_risk_warning.isna(), 'MISSING_STATE', 'is_risk_warning', 'SOURCE_OPTIONAL_STATE_MISSING', 'Preserve missingness')
    add(local.is_risk_warning.eq(1), 'MISSING_STATE', 'is_risk_warning', 'SOURCE_PRESENT_ADAPTER_NOT_CONSUMED', 'Document field contract; do not infer generic ST mapping')
    add(~local.observed_close, 'VALUATION_POLICY_REQUIRED', 'raw_close', 'NO_VERIFIED_REAL_CLOSE', 'Use independent evidence and an explicitly selected policy only for held shares')
    add(local.previous_close.isna(), 'MISSING_PRICE', 'previous_close', 'NO_PREVIOUS_REAL_CLOSE_WITHIN_BOUNDED_SCAN', 'Extend only the affected security observation boundary if needed')
    for field in ['pre_close', 'upper_limit', 'lower_limit']:
        add(local[field].isna() | ~np.isfinite(local[field]) | local[field].le(0),
            'MISSING_PRICE', field, 'PRICE_LIMIT_INPUT_UNAVAILABLE', 'Verify source/rule inputs; no execution permission')
    add(local.pre_close.gt(0) & local.previous_close.gt(0) &
        (local.pre_close / local.previous_close - 1).abs().gt(.001),
        'CORPORATE_ACTION_CANDIDATE', 'pre_close', 'PRE_CLOSE_DIFFERS_FROM_LAST_REAL_CLOSE', 'Review ex-rights or source basis; do not adjust prices')
    add(local.observed_close & local.previous_close.gt(0) &
        (local.raw_close / local.previous_close - 1).abs().gt(.2),
        'CORPORATE_ACTION_CANDIDATE', 'raw_close', 'ABS_RAW_CLOSE_JUMP_GT_20_PERCENT', 'Review action/resumption candidate, not a stock filter')
    add(local.exdr.notna() & local.exdr.ne(0), 'CORPORATE_ACTION_CANDIDATE', 'exdr',
        'SUPPLIER_EXDR_CANDIDATE_SEMANTICS_UNVERIFIED', 'Review supplier action flag')
    prior_member = panel.groupby('symbol').member.shift()
    exits = panel.loc[prior_member.eq(True) & ~panel.member & panel.date.isin(test), KEYS]
    exit_keys = pd.MultiIndex.from_frame(exits)
    add(pd.MultiIndex.from_frame(local[KEYS]).isin(exit_keys), 'PIT_UNVERIFIED', 'member',
        'HISTORICAL_MEMBER_EXIT_NOT_DELISTING_PROOF', 'Keep held securities observable after membership exit')
    for row in duplicates:
        if row['date'] in test:
            risks.append(dict(date=str(row['date'].date()), symbol=row['symbol'], issue_type='SOURCE_IDENTITY_CONFLICT',
                source='BIGQUANT_' + row['source'], source_field='date,symbol', raw_value=str(row['count']),
                research_impact='DUPLICATE_KEY', evidence_available='NONE', sidecar_coverage='NONE',
                recommended_action='Resolve duplicate identity; never choose latest silently', verification_status='SOURCE_OBSERVED',
                knowledge_class='EX_POST_DATA_DIAGNOSTIC', fold=folds[row['date']], member=False,
                holding_exposure='HOLDING_EXPOSURE_NOT_ASSESSED'))
    risk = pd.DataFrame(risks, columns=RISK_COLUMNS)
    gaps, intervals = [], []
    for symbol, part in local.groupby('symbol', sort=True):
        # Runs are measured on the full Test calendar, not only surviving members.
        for _, run in part.groupby(part.observed_close.ne(part.observed_close.shift()).cumsum()):
            first, last = run.iloc[0], run.iloc[-1]
            interval = dict(symbol=symbol, start=str(first.date.date()), end=str(last.date.date()),
                sessions=len(run), status='OBSERVED_RAW_CLOSE' if first.observed_close else 'NO_VERIFIED_REAL_CLOSE')
            intervals.append(interval)
            if first.observed_close:
                continue
            covered = sum((symbol, str(d.date()), 'valuation_mark') in facts for d in run.date)
            gaps.append(dict(**interval, last_real_close_date=None if pd.isna(first.previous_date) else str(first.previous_date.date()),
                last_real_close=None if pd.isna(first.previous_close) else float(first.previous_close),
                next_real_close_date=None if pd.isna(last.next_date) else str(last.next_date.date()),
                next_real_close=None if pd.isna(last.next_close) else float(last.next_close),
                suspension_candidate_sessions=int(run.suspension_proxy.sum()),
                supplier_confirmed_suspended_sessions=int(run.suspended.eq(1).sum()),
                sidecar_covered_sessions=covered, policy_candidate='P9_D01_EXACT_CARRY' if covered else 'NONE',
                approved_for_frozen_p9_01=False, holding_exposure='HOLDING_EXPOSURE_NOT_ASSESSED',
                terminal_missing_candidate=bool(last.date == test[-1]), delisting_status='UNKNOWN'))
    enums = []
    for field in ['suspended', 'st_status', 'is_risk_warning', 'price_limit_status', 'exdr']:
        for value, part in local.groupby(field, dropna=False):
            known = field in ('suspended', 'st_status') and pd.notna(value) and value in (0, 1)
            enums.append(dict(source='BIGQUANT', field=field, raw_value='null' if pd.isna(value) else str(value),
                sessions=len(part), securities=int(part.symbol.nunique()),
                classification='SOURCE_MISSING' if pd.isna(value) else 'ADAPTER_SUPPORTED' if known else
                    'ADAPTER_DISCARDS_SEMANTICS_UNVERIFIED' if field in ('suspended', 'st_status') else 'READABLE_NOT_CONSUMED_SEMANTICS_UNVERIFIED',
                first_date=str(part.date.min().date()), last_date=str(part.date.max().date())))
    return local, risk, pd.DataFrame(gaps), pd.DataFrame(enums), pd.DataFrame(coverage), intervals


def holding_exposure(args, loaded, local, risk, gaps):
    if args.study is None:
        return {'status': 'HOLDING_EXPOSURE_NOT_ASSESSED', 'accounts': []}
    pins = loaded['p9_result']['private_artifact_sha256']
    accounts, affected = [], []
    held_keys, observed_keys = set(), set()
    for account in ('B_factor_score-fold-0', 'B_ridge-fold-0'):
        name = account + '/positions.parquet'
        raw = checked((args.study / name).read_bytes(), pins[name])
        positions = pd.read_parquet(io.BytesIO(raw), columns=KEYS + ['shares'])
        if positions.duplicated(KEYS).any():
            raise ValueError('DUPLICATE_SAVED_POSITION_KEY')
        validate_symbols(sorted(positions.symbol.unique()))
        if not positions.symbol.isin(local.symbol.unique()).all():
            raise ValueError('SAVED_POSITION_OUTSIDE_COHORT')
        joined = positions.merge(local[KEYS + ['observed_close']], on=KEYS, how='left', validate='one_to_one')
        bad = joined.shares.gt(0) & joined.observed_close.eq(False)
        affected.extend(dict(account=account, date=str(r.date.date()), symbol=r.symbol) for r in joined.loc[bad].itertuples())
        held_keys.update((str(r.date.date()), r.symbol) for r in positions.loc[positions.shares.gt(0)].itertuples())
        nav_name = account + '/nav.parquet'
        nav_raw = checked((args.study / nav_name).read_bytes(), pins[nav_name])
        nav = pd.read_parquet(io.BytesIO(nav_raw), columns=['date'])
        if nav.date.duplicated().any() or not positions.date.isin(nav.date).all():
            raise ValueError('SAVED_ACCOUNT_DATE_IDENTITY_CONFLICT')
        account_dates = pd.DatetimeIndex(nav.date)
        # NAV proves returned account dates, including sessions with no positions.
        observed_keys.update((str(d.date()), s) for d in account_dates for s in local.symbol.unique())
        accounts.append(dict(account=account, sha256=digest(raw), nav_sha256=digest(nav_raw),
            first_date=str(nav.date.min().date()), last_date=str(nav.date.max().date()),
            affected_held_security_days=int(bad.sum())))
    risk['holding_exposure'] = ['HELD_IN_SAVED_ACCOUNT' if (d, s) in held_keys else
        'NOT_HELD_IN_ASSESSED_ACCOUNTS' if (d, s) in observed_keys else 'HOLDING_EXPOSURE_NOT_ASSESSED'
        for d, s in zip(risk.date, risk.symbol)]
    for index, row in gaps.iterrows():
        keys = {(str(d.date()), row.symbol) for d in pd.DatetimeIndex(local.loc[
            local.symbol.eq(row.symbol) & local.date.between(row.start, row.end), 'date'])}
        gaps.loc[index, 'holding_exposure'] = ('HELD_IN_SAVED_ACCOUNT' if keys & held_keys else
            'NOT_HELD_IN_ASSESSED_ACCOUNTS' if keys <= observed_keys else 'HOLDING_EXPOSURE_NOT_ASSESSED')
    return dict(status='SAVED_LEDGER_PARTIAL_ACCOUNT_COVERAGE', accounts=accounts,
        affected_held_security_days=affected,
        interrupted_account=dict(account=loaded['p9_result']['interrupted_account'],
            reason=loaded['p9_result']['reason'], exposure='NO_RETURNED_LEDGER; DO_NOT_INFER_HOLDINGS'),
        other_accounts='HOLDING_EXPOSURE_NOT_ASSESSED')


def public_summary(local, risk, gaps, enums, coverage, exposure, entries, intervals, test, symbols):
    by_symbol = []
    for symbol, part in local.groupby('symbol', sort=True):
        own = risk.loc[risk.symbol.eq(symbol)]
        by_symbol.append(dict(symbol=symbol, test_sessions=len(part), member_sessions=int(part.member.sum()),
            price_keys=int(part.price_key.eq(True).sum()), state_keys=int(part.state_key.eq(True).sum()),
            observed_real_closes=int(part.observed_close.sum()), missing_real_closes=int((~part.observed_close).sum()),
            supplier_suspended=int(part.suspended.eq(1).sum()),
            unsupported_status_sessions=int((part.st_status.notna() & ~part.st_status.isin([0, 1])).sum()),
            issues={str(k): int(v) for k, v in own.issue_type.value_counts().items()}))
    ready = (local.observed_valid & local.st_status.isin([0, 1])
        & local.previous_close.gt(0))
    return dict(schema='P9_D02_EXECUTION_PREFLIGHT_V1', status='P9_D02_PREFLIGHT_COMPLETED',
        execution_input_classes=dict(EXECUTION_INPUT_READY=int(ready.sum()),
            KNOWN_NONTRADABLE=int(local.suspended.eq(1).sum())),
        execution_input_ready_meaning='PRICE_AND_CURRENT_ADAPTER_FIELDS_ONLY; NOT_PIT_OR_ORDER_PERMISSION',
        missing_real_closes_in_members=int((~local.observed_close & local.member).sum()),
        missing_bar_in_members=int((~local.price_key.eq(True) & local.member).sum()),
        unsupported_status_in_members=int((local.st_status.notna() & ~local.st_status.isin([0, 1]) & local.member).sum()),
        safety=list(SAFETY), formal_admission=False, live_money=False, execution_ready_certification=False,
        knowledge_class='EX_POST_DATA_DIAGNOSTIC', consumption=['BLOCKING_WARNING', 'VALUATION_EVIDENCE_INDEX'],
        selection_filter_permitted=False, signal_time_information='ONLY_SEPARATELY_VERIFIED_HISTORICAL_CONTRACTS',
        test=dict(first=str(test[0].date()), last=str(test[-1].date()), sessions=len(test), securities=len(symbols),
            grid_rows=len(local), folds={str(k): int(v) for k, v in local.groupby('fold').date.nunique().items()}),
        issues={str(k): int(v) for k, v in risk.issue_type.value_counts().items()},
        observation_classes={str(k): int(v) for k,v in risk.research_impact.value_counts().items()},
        observed_valid_sessions=int(local.observed_valid.sum()), missing_real_close_sessions=int((~local.observed_close).sum()),
        valuation_gap_runs=len(gaps), sidecar_candidates=len(coverage), holdings=exposure, securities=by_symbol,
        source_shards=entries, source_identity='FROZEN_SHARD_BYTES_AND_RECEIPTS_VERIFIED',
        stable_entity_identity='UNKNOWN', code_changes='UNKNOWN', delisting_confirmation='UNKNOWN',
        calendar='BIGQUANT_CN_SOURCE_CALENDAR_NOT_INDEPENDENT_EXCHANGE_CERTIFICATION',
        pit='UNVERIFIED_CURRENT_EXPORT_NOT_HISTORICAL_DELIVERY', hfq='UNVERIFIED_RAW_ONLY',
        ancillary='P9_D01_VALIDATED_EXACT_EVIDENCE_ONLY; NO_BROAD_CSMAR_CNRDS_SCAN',
        interval_count=len(intervals))


def require_private_output(path):
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('PRIVATE_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY')


def publish(private, output):
    """Publish derived risk locations/counts, never commercial observations."""
    require_private_output(private)
    summary = json.loads((private / 'summary.json').read_bytes())
    if summary['safety'] != list(SAFETY) or summary['selection_filter_permitted'] is not False:
        raise ValueError('PRIVACY_OR_FINANCIAL_GATE')
    output.mkdir(parents=True, exist_ok=False)
    summary['private_artifact_sha256'] = {name: digest((private / name).read_bytes()) for name in
        ['execution-risk.csv', 'valuation-gaps.csv', 'status-enums.csv', 'sidecar-coverage.csv',
         'valuation-intervals.json', 'data-coverage.json', 'summary.json']}
    dump(output / 'summary.json', summary)
    dump(output / 'data-coverage.json', summary['securities'])
    risk = pd.read_csv(private / 'execution-risk.csv', dtype={'raw_value': str}, keep_default_na=False)
    risk['raw_value'] = 'REDACTED_COMMERCIAL_OBSERVATION'
    groups = [c for c in RISK_COLUMNS if c != 'date']
    risk = risk.groupby(groups, dropna=False, sort=True).date.agg(
        date='min', end_date='max', affected_sessions='size',
        affected_dates=lambda days: ';'.join(sorted(days))).reset_index()
    risk.to_csv(output / 'execution-risk.csv', index=False)
    gaps = pd.read_csv(private / 'valuation-gaps.csv')
    gaps = gaps.drop(columns=['last_real_close', 'next_real_close'])
    gaps.to_csv(output / 'valuation-gaps.csv', index=False)
    for name in ['status-enums.csv', 'sidecar-coverage.csv']:
        # Enum inventories are aggregate counts, not supplier security-date rows.
        (output / name).write_bytes((private / name).read_bytes())
    intervals = json.loads((private / 'valuation-intervals.json').read_bytes())
    dump(output / 'valuation-intervals.json', intervals)


def run(args):
    started = time.perf_counter()
    require_private_output(args.output)
    config = json.loads(args.config.read_bytes())
    if config['safety'] != list(SAFETY) or config['selection_filter_permitted'] is not False:
        raise ValueError('PRIVACY_OR_FINANCIAL_GATE')
    loaded = frozen_inputs(config)
    loaded['_config'] = config
    bars, status, members, scan, test, folds, entries, documents = load_market(args, loaded)
    evidence = evidence_index(args, loaded, bars, status, documents)
    panel, duplicates = make_panel(bars, status, members, scan, loaded['manifest']['symbols'])
    local, risk, gaps, enums, coverage, intervals = diagnose(panel, test, folds, duplicates, evidence)
    exposure = holding_exposure(args, loaded, local, risk, gaps)
    summary = public_summary(local, risk, gaps, enums, coverage, exposure, entries, intervals, test, loaded['manifest']['symbols'])
    summary['elapsed_seconds'] = time.perf_counter() - started
    summary['scan'] = dict(first=str(scan[0].date()), last=str(scan[-1].date()), sessions=len(scan),
        warmup_sessions=62, tail_sessions=6)
    summary['auxiliary_coverage'] = dict(
        sessions=len(scan) - len(test),
        missing_real_closes=int((~panel.observed_close & ~panel.date.isin(test)).sum()),
        duplicate_source_keys=len(duplicates),
        invalid_ohlc_rows=int((panel.price_key.eq(True) & np.isfinite(panel[PRICES]).all(axis=1) & ~panel.valid_ohlc).sum()),
        price_source_identity_missing=int((panel.price_key.eq(True) & panel.price_sha.isna()).sum()),
        state_source_identity_missing=int((panel.state_key.eq(True) & panel.state_sha.isna()).sum()))
    summary['tool_sha256'] = digest(Path(__file__).read_bytes())
    args.output.mkdir(parents=True, exist_ok=False)
    # Raw values and price-bearing interval details are private artifacts only.
    risk.to_csv(args.output / 'execution-risk.csv', index=False)
    gaps.to_csv(args.output / 'valuation-gaps.csv', index=False)
    enums.to_csv(args.output / 'status-enums.csv', index=False)
    coverage.to_csv(args.output / 'sidecar-coverage.csv', index=False)
    dump(args.output / 'valuation-intervals.json', intervals)
    dump(args.output / 'data-coverage.json', summary['securities'])
    dump(args.output / 'summary.json', summary)
    print(json.dumps(dict(status=summary['status'], test=summary['test'], issues=summary['issues'],
        elapsed_seconds=summary['elapsed_seconds']), ensure_ascii=False), flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'publish':
        parser = argparse.ArgumentParser(description='Publish redacted diagnostic artifacts only')
        parser.add_argument('command', choices=['publish'])
        parser.add_argument('--private', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args()
        publish(args.private, args.output)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/research/p9-d02/preflight.json')
    for name in ('bigquant-root', 'membership-zip', 'manifest', 'source-root', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--study', type=Path)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
