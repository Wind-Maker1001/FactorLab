"""Replay bounded conditional accounts from the pinned D06 accepted history."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_p9_d05 import (
    ROOT, VALIDATION, LEDGER_FIELDS, prepare_replay, advance, qualify_session_prices,
    qualify_valuations, audit_frozen_fees, audit_ledger, checked, dump, file_hash,
)
import numpy as np
import pandas as pd
import psutil
import pyarrow as pa
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.backtest.scenario import ScenarioValuation, ScenarioExecutionSession, LABELS
from factorlab.errors import SchemaError

SYMBOL = '000540.SZ'
CONDITIONAL = 'SCENARIO_CONDITIONAL_NOT_CERTIFIED'
SAFETY = [*LABELS, 'NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
FUND_NOTICE_SHA256 = 'e29722b1992dd642a9e204ea0cb5c14b015f7602ea15f42b997e52e606e0fdbf'


def definitions():
    common = dict(symbol=SYMBOL, start='2023-05-19', end_exclusive='2023-06-15')
    return [
        ScenarioValuation('LAST_CLOSE_CARRY_SENSITIVITY', mark=.40,
            source='Human-defined sensitivity to last observed 2023-05-18 close; not fair value', **common),
        ScenarioValuation('EXTERNAL_FUND_MARK_SENSITIVITY_HINDSIGHT', mark=.11, hindsight=True,
            source_document_sha256=FUND_NOTICE_SHA256,
            source='Penghua non-ETF fund notice published 2023-05-20; counterfactual May 19 close', **common),
        ScenarioValuation('EXTERNAL_FUND_MARK_SENSITIVITY_PUBLICATION_BOUNDED', mark=.11,
            public_after='2023-05-20', before_public_mark=.40, source_document_sha256=FUND_NOTICE_SHA256,
            source='Penghua notice date precision: usable after May 20 end-of-day; prior .40 is a separate assumption', **common),
        ScenarioValuation('ZERO_MARK_STRESS', mark=0.,
            source='Human-defined book-value stress, not cancellation, sale or settlement', **common),
    ]


def qualify_scenario(result, source, evidence, observations, valuation, day):
    """Ordinary evidence for all other holdings; separate identity for assumed marks."""
    mask = result.positions.symbol.eq(valuation.symbol) & result.positions.date.eq(day)
    ordinary = replace(result, positions=result.positions.loc[~mask])
    marks = qualify_valuations(ordinary, source, evidence, None,
                              observations=observations, session_date=day)
    held = result.positions.loc[mask]
    if len(held) != 1 or held.shares.iloc[0] != 133700:
        raise SchemaError('SCENARIO_RIGHTS_REASSESSMENT_REQUIRED')
    row = source.set_index('symbol').loc[valuation.symbol]
    price = valuation.close_mark(day, valuation.symbol, row)
    if price is None or held.mark_price.iloc[0] != price:
        raise SchemaError('SCENARIO_MARK_MISMATCH')
    fills = result.trades
    if (fills.symbol.eq(valuation.symbol) & fills.execution_date.eq(day)).any():
        raise SchemaError('SCENARIO_MARK_CANNOT_EXECUTE')
    nav = result.nav.portfolio_value.iloc[-1]
    assumed = dict(date=day, symbol=valuation.symbol, shares=133700,
        observed_raw_close=None, valuation_price=price, valuation_source_date=valuation.start,
        valuation_policy=valuation.research_id, valuation_status=CONDITIONAL,
        evidence_identity=valuation.identity, affected_portfolio_weight=133700 * price / nav,
        valuation_assumption_used=True, confirmed_suspended=True)
    return pd.DataFrame([*marks.to_dict('records'), assumed], columns=marks.columns)


def consume(state, valuation):
    session, accepted = state['session'], state['prior']
    marks, blocker = [state['prior_marks']], None
    for day in state['dates'][state['dates'] > state['baseline_day']]:
        try:
            snapshot = advance(session, state['plan'])
            candidate = session.result
            source = state['bars'].loc[state['bars'].date.eq(day)]
            qualified = qualify_scenario(candidate, source, state['evidence'],
                                         state['observations'], valuation, day)
            qualify_session_prices(snapshot, source, qualified)
        except SchemaError as exc:
            source = state['bars'].loc[state['bars'].date.eq(day) & state['bars'].symbol.eq(SYMBOL)]
            blocker = dict(date=str(day.date()), reason=str(exc),
                source_state=json.loads(source[['suspended', 'st_status', 'is_st', 'raw_close']].to_json(orient='records')),
                failed_day_not_accepted=True)
            break
        accepted = candidate
        marks.append(qualified)
    return accepted, pd.concat(marks, ignore_index=True), blocker


def summarize(result, prior, valuation, blocker):
    initial, final = prior.nav.iloc[-1], result.nav.iloc[-1]
    added = result.nav.loc[result.nav.date.gt(initial.date)]
    held = result.positions.loc[result.positions.date.eq(final.date) & result.positions.symbol.eq(SYMBOL)].iloc[0]
    series = pd.Series([initial.portfolio_value, *added.portfolio_value])
    trades = result.trades
    new_trades = trades.loc[trades.execution_date.gt(initial.date)]
    rejected = result.rejections.loc[result.rejections.execution_date.gt(initial.date)]
    return dict(research_id=valuation.research_id, assumption_sha256=valuation.identity,
        labels=SAFETY, metric_status=CONDITIONAL, hindsight=valuation.hindsight,
        start=str(added.date.iloc[0].date()) if len(added) else None, end=str(final.date.date()),
        source_sessions=243, completed_source_sessions=len(result.nav)+1, new_scenario_sessions=len(added),
        initial_cash=float(initial.cash), initial_nav=float(initial.portfolio_value),
        end_cash=float(final.cash), end_nav=float(final.portfolio_value),
        first_scenario_close_nav=float(added.portfolio_value.iloc[0]) if len(added) else None,
        held_shares=int(held.shares), assumed_mark=float(held.mark_price),
        held_book_value=float(held.market_value), unsettled_rights_shares=int(held.shares),
        held_unrealized_pnl_change=float(held.market_value - 53480),
        rights_state='ORIGINAL_SHARES_HELD_OTC_REGISTRATION_UNCONFIRMED',
        settlement_cash=None, approved_settlement_entries=0,
        cumulative_fees=float(result.nav.transaction_cost.sum()),
        scenario_fees=float(added.transaction_cost.sum()), executed_orders=len(new_trades),
        cumulative_executed_orders=len(trades), rejected_orders=len(rejected),
        rejection_counts=rejected.reason.value_counts().to_dict(),
        held_rejection_counts=rejected.loc[rejected.symbol.eq(SYMBOL), 'reason'].value_counts().to_dict(),
        conditional_return=float(final.portfolio_value / initial.portfolio_value - 1) if len(added) else None,
        maximum_drawdown=float((series / series.cummax() - 1).min()) if len(added) > 1 else None,
        first_blocker=blocker)


def run(args):
    started = time.perf_counter()
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    checked(args.fund_notice.read_bytes(), FUND_NOTICE_SHA256)
    args.output.mkdir(parents=True, exist_ok=False)
    tracker = ExperimentTracker(args.output / 'experiments')
    pin = json.loads((VALIDATION / 'p9-d06/verification.json').read_bytes())
    checked((args.d06 / 'replay-result.json').read_bytes(), pin['private_replay_receipt_sha256'])
    for name, digest in pin['private_artifacts'].items():
        checked((args.d06 / name).read_bytes(), digest)
    reports, audits, identities = [], [], []
    specs = definitions()
    for spec in specs:
        local = argparse.Namespace(**vars(args))
        local.output = args.output / spec.research_id
        state = prepare_replay(local, continuation=args.d05,
            session_factory=lambda engine, bars, benchmark, **kw: ScenarioExecutionSession(
                engine, bars, benchmark, valuation=spec, **kw))
        for name in (*LEDGER_FIELDS, 'qualifications'):
            actual = state['prior_marks'] if name == 'qualifications' else getattr(state['prior'], name)
            pd.testing.assert_frame_equal(actual, pd.read_parquet(args.d06 / f'fold-0-{name}.parquet'), check_exact=True)
        result, marks, blocker = consume(state, spec)
        audit_frozen_fees(result, state['config'])
        audit = audit_ledger(result, state['bars'].loc[state['bars'].date.le(result.nav.date.iloc[-1])],
                             state['config']['initial_cash'], valuation_prices=marks)
        audit['qualified_marks'] = False
        audit['scenario_marks_reconciled'] = True
        audit['financial_certification'] = False
        for name in LEDGER_FIELDS:
            getattr(result, name).to_parquet(local.output / f'fold-0-{name}.parquet', index=False)
        marks.to_parquet(local.output / 'fold-0-scenario-marks.parquet', index=False)
        receipt = summarize(result, state['prior'], spec, blocker)
        receipt['account_receipts_sha256'] = {p.name: file_hash(p) for p in local.output.iterdir()}
        dump(local.output / 'account-receipt.json', receipt)
        receipt['account_receipt_sha256'] = file_hash(local.output / 'account-receipt.json')
        frozen_identity = json.loads(checked((args.study / 'inference-identity.json').read_bytes(),
            state['frozen_hashes']['inference-identity.json']))
        fold = frozen_identity['folds'][0]
        definition = tracker.create_experiment(spec.research_id, dict(model='B_lightgbm',
            parameters=dict(scenario=asdict(spec), labels=SAFETY, assumption_sha256=spec.identity),
            factors=[], dataset=dict(source_manifest_sha256=state['evidence'].source_manifest_sha256,
                frozen_hashes=state['frozen_hashes'], original_prediction_sha256=state['original_prediction_hash']),
            label=dict(status='FROZEN_NOT_REBUILT'),
            windows=dict(train=fold.get('train'), validation=fold.get('validation'), test=fold['test']),
            walk_forward=dict(fold=0, retrained=False), costs=state['config']['fees']))
        tracked = tracker.append_run(spec.research_id, spec.research_id + '_run',
            code=dict(commit=None, tree=None, source_sha256={str(p.relative_to(ROOT)): file_hash(p)
                for p in (Path(__file__), ROOT / 'tools/research/run_p9_d05.py',
                    ROOT / 'src/factorlab/backtest/execution.py', ROOT / 'src/factorlab/backtest/scenario.py')}),
            environment=dict(python=sys.version, numerical_threads=1), command=sys.argv,
            status='SUCCEEDED', qualification='REAL_CANDIDATE',
            portfolio_metrics={key: receipt[key] for key in ('end_nav', 'conditional_return', 'maximum_drawdown')},
            artifacts=dict(account=dict(path=str((local.output / 'account-receipt.json').resolve()),
                sha256=receipt['account_receipt_sha256'])),
            validation=dict(labels=SAFETY, metric_status=CONDITIONAL,
                blocker=blocker, completed_interval_only=True, baseline_six_tables_exact=True))
        receipt['experiment_definition_sha256'] = definition['sha256']
        receipt['experiment_run_sha256'] = tracked['sha256']
        reports.append(receipt)
        audits.append(dict(research_id=spec.research_id, baseline_six_tables_exact=True,
                           strict_accepted_sessions=45, audit=audit, failed_day_excluded=True))
        identities.append(dict(frozen_hashes=state['frozen_hashes'],
            original_prediction_sha256=state['original_prediction_hash'],
            source_manifest_sha256=state['evidence'].source_manifest_sha256,
            source_shards=state['entries']))
    baseline_report = json.loads((args.d06 / 'replay-result.json').read_bytes())
    # Re-run the ordinary route: no scenario permissions and no extra accepted day.
    local = argparse.Namespace(**vars(args)); local.output = args.output / 'strict-control'
    control = prepare_replay(local, continuation=args.d05)
    from run_p9_d05 import continue_fold
    strict, _, strict_blocker = continue_fold(control['session'], control['dates'][control['dates'] > control['baseline_day']],
        control['plan'], control['bars'], control['evidence'], control['observations'], control['prior'])
    if len(strict.nav) != 44 or strict_blocker['date'] != '2023-05-19':
        raise SchemaError('STRICT_BASELINE_DRIFT')
    for name in LEDGER_FIELDS:
        pd.testing.assert_frame_equal(getattr(strict, name), getattr(control['prior'], name), check_exact=True)
    public = args.output / 'public'; public.mkdir()
    dump(public / 'scenario-definitions.json', dict(schema='P9_S01_SCENARIOS_V1', labels=SAFETY,
        scenarios=[dict(**asdict(s), sha256=s.identity, labels=list(LABELS)) for s in specs],
        valuation_phase='Close only; first scenario open uses prior accepted close; subsequent opens use prior conditional close',
        stop_conditions=['2023-06-15 termination decision: reassess before session',
            '2023-06-30 delisting: no ordinary A-share identity',
            '2023-07-07 rights notice: no proven account registration',
            '2023-08-30 OTC listing suspended: no ordinary A-share execution',
            'Any changed suspension/source state or unverified share event: stop'],
        extension_authorized=False))
    status = ('P9_S01_PARTIAL' if any(r['new_scenario_sessions'] for r in reports)
              and any(r['first_blocker'] and 'SCENARIO_EVENT_REASSESSMENT_REQUIRED' not in r['first_blocker']['reason']
                      for r in reports) else
              'P9_S01_SCENARIO_ACCEPTED' if any(r['new_scenario_sessions'] for r in reports) else 'P9_S01_BLOCKED')
    dump(public / 'scenario-consumption-results.json', dict(schema='P9_S01_CONSUMPTION_V1', status=status,
        labels=SAFETY, strict=dict(status='FINANCIAL_EVIDENCE_BLOCKED', accepted_sessions=45,
            source_sessions=243, last_close='2023-05-18', first_blocker=strict_blocker), scenarios=reports))
    dump(public / 'scenario-ledger-audit.json', dict(schema='P9_S01_LEDGER_AUDIT_V1', scenarios=audits,
        strict_original_receipt_sha256=file_hash(args.d06 / 'replay-result.json'),
        strict_receipt_unchanged=file_hash(args.d06 / 'replay-result.json') == pin['private_replay_receipt_sha256']))
    a = reports[0]
    comparisons = [dict(research_id=r['research_id'], common_end=r['end'],
        end_nav_difference_vs_A=r['end_nav']-a['end_nav'] if r['end'] == a['end'] else None,
        return_difference_vs_A=r['conditional_return']-a['conditional_return']
            if r['conditional_return'] is not None and a['conditional_return'] is not None and r['end'] == a['end'] else None)
        for r in reports]
    dump(public / 'scenario-comparison.json', dict(schema='P9_S01_COMPARISON_V1', metric_status=CONDITIONAL,
        comparisons=comparisons, direct_holding_sensitivity=[dict(mark=p, shares=133700,
        book_value=v, difference_vs_last_close=v-53480, is_portfolio_nav=False)
        for p, v in ((.4, 53480), (.11, 14707), (0., 0))]))
    dump(public / 'remaining-blockers.json', dict(schema='P9_S01_BLOCKERS_V1', labels=SAFETY,
        strict='UNQUALIFIED_HELD_CLOSE: 2023-05-19/000540.SZ',
        scenarios=[dict(research_id=r['research_id'], blocker=r['first_blocker']) for r in reports],
        unresolved=['June 15 decision requires independent reassessment; no indefinite carry authorized',
            'Account-specific 400174 registration quantity, custody and eligibility UNKNOWN',
            'Later share changes, disposal, restructuring and settlement cash unverified'],
        completed_243_sessions=False))
    resources = dict(wall_seconds=time.perf_counter()-started,
        peak_working_set_bytes=psutil.Process().memory_info().peak_wset, python_processes=1,
        numerical_threads=1, output_bytes=sum(p.stat().st_size for p in args.output.rglob('*') if p.is_file()),
        memory_soft_limit_bytes=4*1024**3, temporary_disk_budget_bytes=512*1024**2)
    dump(public / 'verification.json', dict(schema='P9_S01_VERIFICATION_V1', status=status,
        labels=SAFETY, resources=resources, identities=identities, baseline='3877b45a3b4f5830651d30043dad128ae34bf664',
        dependency_pr=114, strict_control_blocker=strict_blocker,
        source_sessions=baseline_report['source_sessions'], frozen_identity_count=128))
    print(json.dumps(dict(status=status, scenarios=reports, resources=resources)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original', 'study', 'd03', 'd04', 'd05', 'd06', 'evidence-root', 'bigquant-root', 'fund-notice', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    run(parser.parse_args())
