"""Bounded held-security event discovery and independent saved-fill accounting."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import zipfile

import pandas as pd
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).parent))
from factorlab.strategies.shared_corporate_actions import CashDividendEvidence, saved_path_accounting
from p9_fee_sensitivity import (compare_fills, load_json, verify_source, verified_file,
                                contract_identities, contracts)

ACCOUNTS = ('B_factor_score-fold-0', 'B_ridge-fold-0')
SAFETY = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


def discover(stock, positions, start, end, sessions):
    """Read only overlapping annual event partitions, predicate-filter held symbols.

    The exact record-close intersection is applied after the Parquet predicate.
    ZIP decompression may read an entire selected member, not the stock archive.
    Missing registration dates are retained if an event overlaps a held session.
    """
    held = pd.concat(positions.values(), ignore_index=True)
    symbols = sorted(held.symbol.unique())
    keys = set(zip(held.date, held.symbol))
    account_keys = {account: set(zip(frame.date, frame.symbol)) for account, frame in positions.items()}
    previous = dict(zip(sessions[1:], sessions[:-1]))
    candidates, native, sources = [], [], []
    with zipfile.ZipFile(stock) as archive:
        for table, ex_field in (('cn_stock_dividend', 'ex_date'), ('cn_stock_allotment', 'exright_date')):
            for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
                member = f'stock/{table}/{year}-01-01_{year}-12-31.parquet'
                raw = archive.read(member)
                frame = pd.read_parquet(BytesIO(raw), filters=[
                    [('instrument', 'in', symbols), ('register_date', '>=', pd.Timestamp(start)),
                     ('register_date', '<', pd.Timestamp(end))],
                    [('instrument', 'in', symbols), (ex_field, '>=', pd.Timestamp(start)),
                     (ex_field, '<', pd.Timestamp(end))]])
                sources.append(dict(member=member, sha256=hashlib.sha256(raw).hexdigest(),
                                    selected_security_rows=len(frame)))
                for index, row in frame.iterrows():
                    record, ex = row.register_date, row[ex_field]
                    if pd.notna(record):
                        relevant = (record, row.instrument) in keys
                    else:
                        relevant = pd.notna(ex) and any((day, row.instrument) in keys for day in
                            (ex, previous.get(ex)))
                    if not relevant:
                        continue
                    event_id = f'BQ:{table}:{year}:{index}'
                    identity = dict(source_member=member, filtered_row_index=int(index),
                                    payload_sha256=sources[-1]['sha256'])
                    native.append(dict(event_id=event_id, identity=identity,
                                       record={k: None if pd.isna(v) else str(v) for k, v in row.items()}))
                    day = lambda v: None if pd.isna(v) else str(pd.Timestamp(v).date())
                    kind = 'RIGHTS_ISSUE' if table == 'cn_stock_allotment' else 'CASH_DIVIDEND'
                    if table == 'cn_stock_dividend':
                        if any(pd.notna(row[k]) and row[k] > 0 for k in ('bonus_rate', 'conversed_rate')):
                            kind = 'CASH_AND_SHARE_CHANGE'
                    candidates.append(dict(event_id=event_id, symbol=row.instrument,
                        event_type=kind, announcement_date=None, vendor_publish_date=day(row.publish_date), public_at=None,
                        record_date=day(record), ex_date=day(ex), payment_date=None,
                        cash_per_share=str(row.cash_before_tax) if table == 'cn_stock_dividend' and pd.notna(row.cash_before_tax) else None,
                        affected_accounts='|'.join(account for account, pairs in account_keys.items()
                            if (record, row.instrument) in pairs or (pd.isna(record) and
                                any((d, row.instrument) in pairs for d in (ex, previous.get(ex))))),
                        source_id=member + ':' + sources[-1]['sha256'],
                        source_record_identity=json.dumps(identity, sort_keys=True),
                        confirmation='VENDOR_CANDIDATE_NOT_IMPLEMENTATION_CERTIFIED',
                        known_at='UNKNOWN', tax_status='TAX_UNVERIFIED',
                        missing='issuer implementation notice; public timestamp; terms/unit confirmation; actual payment/account settlement; account tax; revision chain'))
    # Resolve source duplicates explicitly; a row identity is not an economic ID.
    keys_seen = set()
    for event in candidates:
        key = event['symbol'], event['record_date'], event['ex_date'], event['event_type']
        if key in keys_seen:
            raise ValueError('duplicate economic event candidates require revision resolution')
        keys_seen.add(key)
    return candidates, native, sources


def run(source, stock, private_output, public_output):
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    source, private_output, public_output = map(Path, (source, private_output, public_output))
    evidence = load_json(ROOT / 'docs/validation/p9-01/result.json')
    config, identity = verify_source(source, evidence)
    start, end = identity['folds'][0]['test']
    receipts, positions, source_ids = {}, {}, {}
    for account in ACCOUNTS:
        parts = {}
        for name in ('trades', 'positions', 'nav'):
            relative = account + '/' + name + '.parquet'
            path = verified_file(source, relative, evidence)
            source_ids[relative] = evidence['private_artifact_sha256'][relative]
            parts[name] = pd.read_parquet(path)
        receipts[account] = parts
        positions[account] = parts['positions']
    sessions = pd.DatetimeIndex(sorted(set().union(*(set(p['nav'].date) for p in receipts.values()))))
    candidates, native, shards = discover(stock, positions, start, end, sessions)
    events = [CashDividendEvidence(**{k: v for k, v in row.items()
              if k in CashDividendEvidence.__dataclass_fields__}) for row in candidates]
    private_output.mkdir(parents=True, exist_ok=False)
    public_output.mkdir(parents=True, exist_ok=False)
    (private_output / 'native-candidates.json').write_text(json.dumps(native, ensure_ascii=False, indent=2), encoding='utf-8')
    windows = []
    session_number = {day: i for i, day in enumerate(sessions)}
    for account, frame in positions.items():
        for symbol, rows in frame.groupby('symbol'):
            dates = sorted(rows.loc[rows.shares.gt(0), 'date'])
            first = last = None
            for day in dates:
                if last is not None and session_number[day] != session_number[last] + 1:
                    windows.append(dict(account=account, symbol=symbol, start=first, end=last))
                    first = None
                if first is None:
                    first = day
                last = day
            if first is not None:
                windows.append(dict(account=account, symbol=symbol, start=first, end=last))
    pd.DataFrame(windows).to_csv(private_output / 'required-held-security-windows.csv', index=False)
    reconciliation, fees, entitlement_summary, accounts = [], [], [], []
    for account, parts in receipts.items():
        selected_events = [event for event, candidate in zip(events, candidates)
                           if account in candidate['affected_accounts'].split('|')]
        result = saved_path_accounting(parts['nav'], parts['trades'], parts['positions'], selected_events,
                                       initial_cash=config['initial_cash'])
        directory = private_output / account
        directory.mkdir()
        for name in ('ledger', 'journal', 'entitlements', 'unresolved'):
            result[name].to_csv(directory / (name + '.csv'), index=False)
        detail, costs = compare_fills(parts['trades'], start=start, end=end)
        detail.to_csv(directory / 'fill-fee-sensitivity.csv', index=False)
        for name, components in costs['fees'].items():
            fees.append(dict(account=account, contract=name, **components,
                             scope='SAME_FILL_FEE_DIAGNOSTIC_NOT_PORTFOLIO_RETURN', dividend_tax=None))
        table = result['ledger']
        max_residual = max(float(table[c].abs().max()) for c in ('cash_residual', 'equity_residual', 'dividend_residual'))
        if max_residual > 1e-7:
            raise ValueError('corporate-action overlay reconciliation failed')
        reconciliation.append(dict(account=account, saved_execution_sessions=len(table),
            trade_cash_flow=float(table.trade_cash_flow.sum()), transaction_cost=float(table.transaction_cost.sum()),
            posted_verified_dividend_cash=float(table.paid_cash_gross.iloc[-1]),
            actual_dividend_cash_total=None, dividend_receivable_total=None, dividend_tax_total=None,
            max_cash_residual=float(table.cash_residual.abs().max()),
            max_equity_residual=float(table.equity_residual.abs().max()),
            max_dividend_residual=float(table.dividend_residual.abs().max()),
            status='POSTED_SUBLEDGER_RECONCILED_FULL_CORPORATE_ACTIONS_UNKNOWN'))
        affected = sum((pd.Timestamp(e['record_date']), e['symbol']) in set(zip(parts['positions'].date, parts['positions'].symbol))
                       for e in candidates if e['record_date'])
        entitlement_summary.append(dict(account=account, held_candidate_events=affected,
            verified_entitlements=len(result['entitlements']), gross_entitlement=None,
            status='EVENT_UNRESOLVED', eligible_quantity_public='PRIVATE'))
        accounts.append(dict(account=account, event_status='BLOCKED_EVIDENCE',
            original_price_result='PRESERVED_CONDITIONAL_RAW_PRICE_NET_TRADING_COSTS',
            corporate_action_sensitive_return=None, investor_total_return=None))
    public_events = [{k: v for k, v in e.items() if k not in ('source_id',)} for e in candidates]
    # Raw economic terms are withheld until an implementation notice establishes them.
    for row in public_events:
        row['cash_per_share'] = None
        row['economic_terms_status'] = 'VENDOR_CANDIDATE_PRIVATE_PENDING_ISSUER_CONFIRMATION'
    pd.DataFrame(public_events).to_csv(public_output / 'corporate-action-events.csv', index=False)
    pd.DataFrame(entitlement_summary).to_csv(public_output / 'entitlement-ledger.csv', index=False)
    pd.DataFrame(reconciliation).to_csv(public_output / 'cashflow-reconciliation.csv', index=False)
    pd.DataFrame(fees).to_csv(public_output / 'fee-sensitivity.csv', index=False)
    unresolved = [dict(event_id=e['event_id'], symbol=e['symbol'], record_date=e['record_date'],
                       ex_date=e['ex_date'], status='EVENT_UNRESOLVED', minimum_evidence=e['missing']) for e in candidates]
    unresolved.append(dict(event_id='COVERAGE', symbol='HELD_SECURITY_WINDOWS', status='UNKNOWN',
        minimum_evidence='complete held-window all-action index including cancellations, delisting and succession; opening/trailing entitlements'))
    pd.DataFrame(unresolved).to_csv(public_output / 'unresolved-events.csv', index=False)
    result = dict(schema='P9_C02_SAVED_PATH_V1', engineering_status='IMPLEMENTED',
        valuation_qualification='P9_D02_CONDITIONAL_VALUATION_NOT_REEXECUTION',
        valuation_evidence='docs/validation/p9-d02/evidence-consumption.json',
        financial_certification='BLOCKED_EVIDENCE', safety=SAFETY, accounts=accounts,
        candidate_events=len(candidates), affected_symbols=sorted({e['symbol'] for e in candidates}),
        complete_implementation_and_payment_events=0, total_return=None,
        other_models_and_folds='NOT_RUN', original_source_receipts=source_ids,
        event_shards=shards, fee_contracts=contract_identities(contracts()),
        evidence_scope=dict(BigQuant='FOUR_OVERLAPPING_ANNUAL_PARTITIONS_HELD_RECORD_CLOSE_INTERSECTION',
            CSMAR='EXISTING_601857_2017_2018_EXTRACT_NOT_APPLICABLE_NO_FULL_ARCHIVE_SCAN',
            CNRDS='PR80_EXISTING_TARGET_EVIDENCE_NOT_APPLICABLE_NO_UNBOUNDED_WORKBOOK_SCAN',
            issuer_notices='NO_IMPLEMENTATION_OR_ACCOUNT_PAYMENT_EVIDENCE_BOUND_FOR_THESE_CANDIDATES'),
        limits=dict(processes=1, memory_soft_limit_gib=8),
        return_basis='BOUNDED_POSTED_SUBLEDGER_NOT_INVESTOR_TOTAL_RETURN')
    (public_output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps(dict(candidate_events=len(candidates), affected_symbols=result['affected_symbols'],
                          accounts=entitlement_summary, reconciliation=reconciliation), ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source', 'stock', 'private-output', 'public-output'):
        parser.add_argument('--' + flag, required=True, type=Path)
    args = parser.parse_args()
    run(args.source, args.stock, args.private_output, args.public_output)
