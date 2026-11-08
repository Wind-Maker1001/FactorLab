"""Bounded PetroChina evidence diagnostics; run with python -I -X utf8."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, localcontext
from pathlib import Path
import zipfile

import pandas as pd


def cash_candidates(previous: str, cash: str) -> dict[str, str]:
    """Candidate relationships, not a vendor algorithm contract."""
    with localcontext() as ctx:
        ctx.prec = 40
        p, d = Decimal(previous), Decimal(cash)
        q = (p - d) / p
        return {'ex_reference': str(p - d), 'forward': str(q),
                'backward': str(1 / q)}


def selected(frame, column):
    dates = pd.to_datetime(frame[column])
    return frame.loc[dates.between('2017-01-03', '2018-12-28')].copy()


def run(candidate: Path, stock_zip: Path, evidence: Path) -> dict:
    native = candidate / 'provenance'
    cs = selected(pd.read_parquet(native / 'TRDNEW_Cptl-0-native.parquet'), 'Exdistdt')
    factors = selected(pd.read_parquet(native / 'TRD_AdjustFactor-0-native.parquet'), 'TradingDate')
    bars = pd.read_parquet(candidate / 'market/bars.parquet')
    tables = {}
    members = {}
    with zipfile.ZipFile(stock_zip) as archive:
        for table in ('cn_stock_dividend', 'cn_stock_allotment', 'cn_stock_adjust_factor'):
            names = [n for n in archive.namelist() if n.startswith(f'stock/{table}/')
                     and n.endswith('.parquet') and n.split('/')[-1][:4] in ('2017', '2018')]
            pieces = []
            for name in names:
                frame = pd.read_parquet(archive.open(name))
                pieces.append(frame.loc[frame.instrument == '601857.SH'])
            tables[table] = selected(pd.concat(pieces, ignore_index=True), 'date')
            members[table] = names
    bq = tables['cn_stock_dividend']
    bf = tables['cn_stock_adjust_factor']
    cs_dates = set(cs.Exdistdt.astype(str))
    bq_dates = set(pd.to_datetime(bq.ex_date).dt.strftime('%Y-%m-%d'))
    matched = 0
    for row in cs.itertuples():
        other = bq.loc[pd.to_datetime(bq.ex_date) == pd.Timestamp(row.Exdistdt)]
        if len(other) == 1 and abs(Decimal(str(other.iloc[0].cash_before_tax)) - Decimal(str(row.Amount))) < Decimal('1e-14'):
            matched += 1
    comparison = []
    for row in cs.itertuples():
        day = str(row.Exdistdt)
        prior = bars.loc[pd.to_datetime(bars.date) < pd.Timestamp(day)].sort_values('date').iloc[-1]
        # Binary64 input is represented, not silently made exact vendor decimal data.
        previous = str(prior.raw_close)
        calc = cash_candidates(previous, str(row.Amount))
        cf = factors.loc[factors.TradingDate.astype(str) == day].iloc[0]
        observed = bf.loc[pd.to_datetime(bf.date) == pd.Timestamp(day)]
        forward = observed.loc[observed.type == 'forward'].iloc[0]
        backward = observed.loc[observed.type == 'backward'].iloc[0]
        comparison.append({'event_date': day, 'candidate_forward_minus_csmar':
                           str(Decimal(calc['forward']) - Decimal(str(cf.FwardFactor))),
                           'bigquant_minus_csmar_forward': str(Decimal(str(forward.adjust_factor)) - Decimal(str(cf.FwardFactor))),
                           'bigquant_minus_csmar_backward': str(Decimal(str(backward.adjust_factor)) - Decimal(str(cf.BwardFactor))),
                           'bigquant_method': str(forward.method)})
    notices = json.loads((evidence / 'corroborated-events-final.json').read_text(encoding='utf-8'))
    notice_checks = []
    for entry in notices:
        official = entry['official_evidence']
        text = (evidence / f"official-{official['name']}.txt").read_text(encoding='utf-8')
        date = pd.Timestamp(official['effective_date'])
        registration = pd.Timestamp(official['record_date'])
        notice_checks.append({'notice': official['name'],
                              'cash_text_match': official['cash_per_share'] in text,
                              'ex_date_text_match': f'{date.year}/{date.month}/{date.day}' in text,
                              'record_date_text_match': f'{registration.year}/{registration.month}/{registration.day}' in text,
                              'historical_pdf_sha256_reference': official['sha256'],
                              'retrieved_at_reference': official['retrieved_at']})
    return {'schema': 'P3D03_BOUNDED_DIAGNOSTIC_V1', 'security': '601857.SH',
            'window': ['2017-01-03', '2018-12-28'], 'status': 'P3D03_PARTIAL',
            'restrictions': ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED'],
            'counts': {'csmar_cash_candidates': len(cs), 'bigquant_dividend_candidates': len(bq),
                       'matching_cash_date_pairs_binary64_tolerance_1e_14': matched,
                       'csmar_only_dates': len(cs_dates - bq_dates), 'bigquant_only_dates': len(bq_dates - cs_dates),
                       'csmar_factor_rows': len(factors), 'bigquant_factor_rows': len(bf),
                       'bigquant_allotment_rows': len(tables['cn_stock_allotment']),
                       'bigquant_null_bonus_rate': int(bq.bonus_rate.isna().sum()),
                       'bigquant_null_conversed_rate': int(bq.conversed_rate.isna().sum())},
            'coverage': 'UNKNOWN', 'vendor_historical_available_at': 'UNKNOWN',
            'revisions': 'UNKNOWN', 'source_members': members,
            'factor_differences_derived_not_vendor_rows': comparison,
            'notice_text_checks': notice_checks,
            'hash_policy': 'HISTORICAL_RECEIPT_REFERENCES_ONLY; INPUTS_READ_ONLY; NO_REHASH'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--stock-zip', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.candidate, args.stock_zip, args.evidence)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write('\n')
