"""Verify fixed P9-C02 events against issuer originals and frozen account receipts."""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

import pandas as pd
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(Path(__file__).parent))
from factorlab.strategies.shared_corporate_actions import CashDividendEvidence, saved_path_accounting
from p9_fee_sensitivity import load_json, verify_source, verified_file

ACCOUNTS = ('B_factor_score-fold-0', 'B_ridge-fold-0')
SAFETY = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']


def run(source, prior, private, output):
    started = time.perf_counter()
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    source, prior, private, output = map(Path, (source, prior, private, output))
    private.mkdir(exist_ok=False, parents=True)
    frozen = load_json(ROOT / 'docs/validation/p9-01/result.json')
    config, _ = verify_source(source, frozen)
    baseline = pd.read_csv(ROOT / 'docs/validation/p9-c02/corporate-action-events.csv').set_index('event_id')
    native = {r['event_id']: r for r in load_json(prior / 'native-candidates.json')}
    evidence = load_json(output / 'event-evidence.json')
    if {r['event_id'] for r in evidence} != set(baseline.index) or len(evidence) != 9:
        raise ValueError('fixed event identity mismatch')
    for row in evidence:
        old = baseline.loc[row['event_id']]
        for field in ('symbol', 'record_date', 'ex_date'):
            if old[field] != row[field]:
                raise ValueError('candidate identity/date mismatch')
        if hashlib.sha256((output / row['original']).read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('issuer original identity mismatch')
        candidate = native[row['event_id']]['record']
        if Decimal(candidate['cash_before_tax']) != Decimal(row['cash_per_share']):
            raise ValueError('vendor/issuer terms require explicit resolution')
    summaries, detail, conservation = [], [], []
    for account in ACCOUNTS:
        parts = {name: pd.read_parquet(verified_file(source, account + '/' + name + '.parquet', frozen))
                 for name in ('nav', 'trades', 'positions')}
        nav, trades, positions = (parts[k] for k in ('nav', 'trades', 'positions'))
        events = []
        account_detail = []
        for row in evidence:
            if account not in baseline.loc[row['event_id'], 'affected_accounts'].split('|'):
                continue
            record = pd.Timestamp(row['record_date'])
            days = pd.DatetimeIndex(nav.date)
            if record not in days or record.dayofweek >= 5:
                raise ValueError('registration date is not a saved trading session')
            held = positions.loc[positions.symbol.eq(row['symbol']) & positions.date.eq(record)]
            if len(held) != 1:
                raise ValueError('unique record-close holding required')
            shares = int(held.iloc[0].shares)
            signed = trades.loc[trades.symbol.eq(row['symbol']) & trades.execution_date.le(record)].copy()
            signed['quantity'] = signed.executed_shares.where(signed.side.eq('buy'), -signed.executed_shares)
            reconstructed = int(signed.quantity.sum())
            if shares != reconstructed or shares <= 0:
                raise ValueError('record-close quantity does not reconcile to frozen fills')
            day_fills = signed.loc[signed.execution_date.eq(record)]
            gross = Decimal(shares) * Decimal(row['cash_per_share'])
            proof = row['announcement_id'] + ':' + row['sha256']
            events.append(CashDividendEvidence(event_id=row['event_id'], symbol=row['symbol'],
                source_id=proof, announcement_date=row['public_date'], public_at=row['public_date'],
                public_time_precision='DAY', record_date=row['record_date'], ex_date=row['ex_date'],
                cash_per_share=row['cash_per_share'], implementation_evidence_id=proof,
                payment_date=row['payment_date'], issuer_payment_evidence_id=proof + ':payment',
                settlement_rule_evidence_id=proof + ':distribution-method'))
            item = dict(account=account, event_id=row['event_id'], symbol=row['symbol'],
                record_date=row['record_date'], ex_date=row['ex_date'], payment_date=row['payment_date'],
                valid_saved_session=True, record_day_trades_included=True,
                record_day_fill_count=len(day_fills), record_day_net_quantity=int(day_fills.quantity.sum()),
                eligible_shares=shares, reconstructed_shares=reconstructed,
                cash_per_share=row['cash_per_share'], gross_receivable=str(gross),
                entitlement_verified=True, simulated_settlement_by_rule=True,
                actual_account_payment_verified=False, actual_cash_received=None,
                tax=None, after_tax_cash=None, event_completeness='EVENT_COMPLETENESS_UNKNOWN')
            account_detail.append(item)
        actual = saved_path_accounting(nav, trades, positions, events, initial_cash=config['initial_cash'])
        simulated = saved_path_accounting(nav, trades, positions, events,
            initial_cash=config['initial_cash'], settlement_basis='SIMULATED_BY_RULE')
        directory = private / account
        directory.mkdir()
        for basis, result in (('receivable', actual), ('simulated', simulated)):
            for name in ('ledger', 'journal', 'entitlements', 'unresolved'):
                result[name].to_csv(directory / (basis + '-' + name + '.csv'), index=False)
        total = sum((Decimal(r['gross_receivable']) for r in account_detail), Decimal(0))
        ledger = simulated['ledger']
        last = ledger.iloc[-1]
        actual_last = actual['ledger'].iloc[-1]
        independent = dict(account=account, expected_gross_income=str(total),
            recognized_income=float(last.dividend_income_gross), receivable_only=float(actual_last.receivable_gross),
            actual_posted_cash=float(actual_last.paid_cash_gross), simulated_gross_cash=float(last.simulated_cash_gross),
            simulated_unpaid_receivable=float(last.receivable_gross), actual_account_cash_received=None,
            tax=None, after_tax_cash=None,
            max_cash_residual=float(ledger.cash_residual.abs().max()),
            max_equity_residual=float(ledger.equity_residual.abs().max()),
            max_income_transfer_residual=float(ledger.dividend_residual.abs().max()),
            max_position_quantity_residual=max(abs(r['eligible_shares']-r['reconstructed_shares']) for r in account_detail),
            independent_income_residual=float(total)-float(last.dividend_income_gross),
            independent_transfer_residual=float(total)-float(last.simulated_cash_gross)-float(last.receivable_gross),
            original_trade_count=len(trades), original_trade_cost=float(trades.transaction_cost.sum()))
        if any(abs(independent[k]) > 1e-7 for k in ('max_cash_residual', 'max_equity_residual',
                'max_income_transfer_residual', 'max_position_quantity_residual',
                'independent_income_residual', 'independent_transfer_residual')):
            raise ValueError('independent accounting conservation failed')
        detail.extend(account_detail)
        conservation.append(independent)
        summaries.append(dict(account=account, verified_intersections=len(account_detail),
            gross_verified_receivable=str(total), gross_simulated_settlement=str(total),
            actual_account_payment_verified=False, actual_cash_received=None, tax=None, after_tax_cash=None))
    if len(detail) != 14 or any(r['verified_intersections'] != 7 for r in summaries):
        raise ValueError('frozen account/event intersection mismatch')
    pd.DataFrame(detail).to_csv(private / 'entitlement-detail.csv', index=False)
    # Preserve the established private quantity boundary; publish aggregate money only.
    public_detail = [{k: v for k, v in r.items() if k not in (
        'eligible_shares', 'reconstructed_shares', 'record_day_net_quantity', 'gross_receivable')}
        for r in detail]
    pd.DataFrame(public_detail).to_csv(output / 'account-entitlement-verification.csv', index=False)
    pd.DataFrame(summaries).to_csv(output / 'account-summary.csv', index=False)
    pd.DataFrame(conservation).to_csv(output / 'accounting-conservation.csv', index=False)
    matrix = [{k: v for k, v in r.items() if k not in ('issuer_index_titles', 'issuer_index_pages',
        'revision_candidates')} for r in evidence]
    pd.DataFrame(matrix).to_csv(output / 'event-evidence-matrix.csv', index=False)
    windows = pd.read_csv(prior / 'required-held-security-windows.csv')
    admission = pd.read_csv(ROOT / 'docs/validation/dq-c01/data-admission-matrix.csv').fillna('')
    selected = admission.loc[admission.apply(lambda r: any(term in ' '.join(map(str, r)) for term in
        ('cn_stock_dividend', 'cn_stock_allotment', '分红', '股本变动', '退市', '换股')), axis=1)]
    selected.to_csv(output / 'existing-source-coverage.csv', index=False)
    coverage = dict(status='EVENT_COMPLETENESS_UNKNOWN', held_intervals=len(windows),
        held_symbols=int(windows.symbol.nunique()), accounts=sorted(windows.account.unique()),
        window_manifest_sha256=hashlib.sha256((prior/'required-held-security-windows.csv').read_bytes()).hexdigest(),
        checked='Saved held intervals; P9-C02 dividend/allotment shard receipts; DQ-03 event relations; DQ-C01 table metadata; nine issuer bounded announcement indexes',
        newly_read_vendor_shards=0, original_p9_c02_dividend_allotment_shards=4,
        missing='No held-window complete all-action index covering share distributions, rights, mergers, delisting, cancellations and opening/trailing entitlements; issuer indexes cover only nine issuers and 2023-05-01/2023-08-15, not all held intervals')
    (output/'event-completeness.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    result = dict(schema='P9_C03_EVIDENCE_V1', status='W01_PARTIAL', safety=SAFETY,
        issuer_implementation_verified_events=9, payment_date_verified_events=9,
        entitlement_verified_intersections=14, actual_account_payment_verified=False,
        tax_status='TAX_UNVERIFIED', event_completeness='EVENT_COMPLETENESS_UNKNOWN',
        limited_nine_event_gross_scenario_eligible=True, complete_portfolio_total_return_eligible=False,
        investor_total_return=None, after_tax_return=None, accounts=summaries,
        source_receipts={k: v for k, v in frozen['private_artifact_sha256'].items()
            if any(k.startswith(a + '/') for a in ACCOUNTS)
            and k.endswith(('nav.parquet', 'trades.parquet', 'positions.parquet'))},
        elapsed_seconds=time.perf_counter()-started,
        resources=dict(processes=1,arrow_cpu_threads=1,arrow_io_threads=1,
            memory_soft_limit_bytes=4*1024**3,temporary_disk_budget_bytes=512*1024**2,
            issuer_original_bytes=sum(r['notice_bytes'] for r in evidence),new_vendor_shards_read=0),
        regression_status='PENDING')
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(status=result['status'],accounts=summaries,conservation=conservation),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','prior','private','output'):
        parser.add_argument('--'+name, required=True)
    run(**vars(parser.parse_args()))
