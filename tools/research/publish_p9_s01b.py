"""Verify repeated bounded accounts and publish aggregate research evidence."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd

from run_p9_s01 import definitions, SAFETY
from run_p9_d05 import dump, file_hash, LEDGER_FIELDS


def publish(args):
    output = args.output
    report = json.loads((args.primary / 'public/scenario-consumption-results.json').read_bytes())
    audits = json.loads((args.primary / 'public/scenario-ledger-audit.json').read_bytes())
    verification = json.loads((args.primary / 'public/verification.json').read_bytes())
    comparisons, checks = [], []
    reference = None
    for spec, receipt in zip(definitions(), report['scenarios'], strict=True):
        primary, repeat, old = (root / spec.research_id for root in (args.primary, args.repeat, args.s01))
        for name in (*LEDGER_FIELDS, 'scenario-marks'):
            current = pd.read_parquet(primary / f'fold-0-{name}.parquet')
            pd.testing.assert_frame_equal(current, pd.read_parquet(repeat / f'fold-0-{name}.parquet'), check_exact=True)
            earlier = pd.read_parquet(old / f'fold-0-{name}.parquet')
            date_field = 'execution_date' if name in ('trades', 'rejections') else 'date'
            if date_field not in current.columns:
                date_field = 'execution_date'
            pd.testing.assert_frame_equal(current.loc[current[date_field].le('2023-05-19')].reset_index(drop=True),
                                          earlier.reset_index(drop=True), check_exact=True)
        positions = pd.read_parquet(primary / 'fold-0-positions.parquet')
        nav = pd.read_parquet(primary / 'fold-0-nav.parquet')
        trades = pd.read_parquet(primary / 'fold-0-trades.parquet')
        marks = pd.read_parquet(primary / 'fold-0-scenario-marks.parquet')
        final = positions.loc[positions.date.eq('2023-06-14')]
        held = positions.loc[positions.symbol.eq('000540.SZ') & positions.date.ge('2023-05-19')]
        assert held.shares.eq(133700).all()
        assert not (trades.symbol.eq('000540.SZ') & trades.execution_date.ge('2023-05-19')).any()
        assert receipt['completed_source_sessions'] == 64 and receipt['end'] == '2023-06-14'
        assert receipt['first_blocker']['date'] == '2023-06-15'
        receipt['new_sessions_since_s01'] = 18
        receipt['other_holdings'] = json.loads(final.loc[final.symbol.ne('000540.SZ')].to_json(orient='records', date_format='iso'))
        receipt['daily_conditional_nav'] = json.loads(nav.loc[nav.date.ge('2023-05-18')].to_json(orient='records', date_format='iso'))
        assumed = marks.loc[marks.symbol.eq('000540.SZ') & marks.date.ge('2023-05-19')]
        receipt['first_011_consumption'] = str(assumed.loc[assumed.valuation_price.eq(.11), 'date'].min().date()) if assumed.valuation_price.eq(.11).any() else None
        receipt['ledger_receipt_path'] = str(primary)
        if reference is None:
            reference = receipt
        direct = receipt['held_book_value'] - reference['held_book_value']
        delta = receipt['end_nav'] - reference['end_nav']
        fees = receipt['scenario_fees'] - reference['scenario_fees']
        comparisons.append(dict(research_id=spec.research_id, nav_difference_vs_A=delta,
            direct_000540_mark_difference=direct, net_path_difference=delta-direct,
            extra_fees_vs_A=fees, gross_other_holdings_path_difference=delta-direct+fees,
            explanation='Gross residual is realized/unrealized market P&L on different holdings and execution paths; fees include frozen slippage. Not a pure mark impairment.'))
        checks.append(dict(research_id=spec.research_id, repeated_six_tables_exact=True,
            original_s01_six_tables_exact=True, constant_133700_shares=True, no_suspended_fills=True))
    suites = ET.parse(args.tests).getroot()
    tests = sum(int(s.attrib.get('tests', 0)) for s in suites.iter('testsuite'))
    failures = sum(int(s.attrib.get('failures', 0)) + int(s.attrib.get('errors', 0)) for s in suites.iter('testsuite'))
    assert failures == 0
    status = 'P9_S01B_BOUNDED_SCENARIO_ACCEPTED'
    report.update(schema='P9_S01B_RESULTS_V1', status=status)
    verification.update(schema='P9_S01B_VERIFICATION_V1', status=status, baseline=args.baseline,
        dependency_prs=[109, 114, 117], tests=dict(count=tests, failures=failures),
        checks=checks, state_contract_sha256=file_hash(output / 'state-contract.json'),
        repeat_resources=json.loads((args.repeat / 'public/verification.json').read_bytes())['resources'],
        private_primary=str(args.primary), private_repeat=str(args.repeat),
        source_sha256={name: file_hash(Path(__file__).resolve().parents[2] / name) for name in (
            'src/factorlab/data/market_evidence_sidecar.py',
            'src/factorlab/research/oos_execution_recovery.py',
            'tools/research/run_p9_d05.py', 'tools/research/run_p9_s01.py',
            'tools/research/publish_p9_s01b.py')})
    dump(output / 'scenario-results.json', report)
    dump(output / 'scenario-comparison.json', dict(labels=SAFETY, comparisons=comparisons,
        budget_timing='Open uses prior accepted conditional close. B2 May 22 open still uses .40; May 22 close first consumes .11. B1 May 22 open already uses .11.'))
    dump(output / 'ledger-verification.json', dict(**audits, checks=checks))
    dump(output / 'remaining-blockers.json', dict(labels=SAFETY, bounded_window_completed=True,
        strict_first_blocker=report['strict']['first_blocker'], scenario_first_blocker=report['scenarios'][0]['first_blocker'],
        remaining=['June 15 reassessment required', 'Account-specific 400174 registration and later share/settlement events remain unqualified']))
    dump(output / 'verification.json', verification)
    print(json.dumps(dict(status=status, tests=tests, comparisons=comparisons)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('primary', 'repeat', 's01', 'tests', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--baseline', required=True)
    publish(parser.parse_args())
