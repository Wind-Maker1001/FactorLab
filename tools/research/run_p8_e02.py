"""SYNTHETIC / ENGINEERING_ONLY / NOT_FINANCIAL_EVIDENCE; offline shared account."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import socket

import pandas as pd

from factorlab.evaluation import EvaluationContext, ResearchEvaluation
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from tests.research.test_portfolio_dividend_execution import (
    LABELS, SYMBOLS, execute, reconcile, scenario,
)

HEAD = 'fc0fa9412af6f3fa33765b10476213ad7a784941'
TREE = '64be64d6afe212a71903a56e020aa2fd46a54c6e'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def table(frame):
    return json.loads(frame.to_json(orient='table', date_format='iso', double_precision=15))


def run(output):
    provenance = capture_provenance(Path(__file__).resolve().parents[2])
    output.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        (output / name).write_text(json.dumps(dict(labels=LABELS, **value), indent=2,
            default=str, allow_nan=False)+'\n', encoding='utf-8')

    inputs = scenario(output / 'synthetic-evidence')
    days, bars, targets, benchmark, config, fees, plans = inputs
    manifest = dict(seed=0, random_generation=False, symbols=SYMBOLS,
        security_identity='RESERVED FICTIONAL FIXTURE IDENTIFIERS; NOT LISTING EVIDENCE',
        initial_cash=config.initial_cash, initial_positions=[],
        calendar=list(days.strftime('%Y-%m-%d')), bars=table(bars), targets=table(targets),
        benchmark=table(benchmark), config=asdict(config), fees=[asdict(rule) for rule in fees.rules],
        events=[asdict(event) for plan in plans.values() for event in plan.snapshot.events],
        taxes=[asdict(tax) for plan in plans.values() for tax in plan.taxes],
        dividend_plans={symbol: dict(scope=plan.scope, snapshot=asdict(plan.snapshot),
            supplements=[asdict(s) for s in plan.supplements]) for symbol, plan in plans.items()},
        payment_policy='DATE_ONLY_AFTER_OPEN_ORDERS; AVAILABLE_NEXT_OBSERVED_OPEN',
        executor='AShareExecutionBacktester.run_target_weights -> _iterate_selected -> _execute_order',
        code=dict(starting_commit=HEAD, starting_tree=TREE), labels=LABELS)
    # Evidence file locations do not participate in the frozen economic identity.
    identity = digest({key: value for key, value in manifest.items() if key != 'dividend_plans'})
    save('synthetic-scenario.json', dict(configuration_sha256=identity, manifest=manifest))
    result, repeated = execute(inputs), execute(inputs)
    hashes = {}
    for name in ('nav', 'positions', 'trades', 'rejections', 'signals', 'target_comparison',
                 'entitlements', 'recognitions', 'payments', 'return_accounting'):
        frame = getattr(result, name)
        pd.testing.assert_frame_equal(frame, getattr(repeated, name), check_exact=True)
        hashes[name] = digest(table(frame))
        save(name+'.json', dict(table=table(frame), sha256=hashes[name]))
    residuals = reconcile(result, config, fees)
    assert len(result.nav) == 12 and result.nav.holdings.max() >= 2
    assert len(result.entitlements) == len(result.recognitions) == len(result.payments) == 4
    assert result.rejections.reason.eq('cash_constraint').any()
    assert result.payments.gross.sum() > 0
    empty_inputs = scenario(output / 'empty-plan-evidence', empty=True)
    plain, empty = execute(empty_inputs, plans=False), execute(empty_inputs)
    pd.testing.assert_frame_equal(plain.nav, empty.nav[plain.nav.columns], check_exact=True)
    for name in ('positions', 'trades', 'signals', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(plain, name), getattr(empty, name), check_exact=True)
    context = EvaluationContext(identity, 'SYNTHETIC_MULTI_SECURITY', 'ACCOUNT_NAV', identity,
        'SYNTHETIC_FLAT_BENCHMARK', tuple(days.strftime('%Y-%m-%d')), 1)
    evaluated = ResearchEvaluation(context).portfolio('p8-e02', result.nav, trades=result.trades)
    save('evaluation.json', evaluated.to_dict())
    tracker = ExperimentTracker(output / 'experiments')
    definition = dict(model='FROZEN_TARGETS', parameters={'configuration_sha256': identity,
        'dividend_policy': result.dividend_policy, 'labels': LABELS}, factors=[],
        dataset={'sha256': identity, 'source': 'SYNTHETIC'}, label={'name': 'ACCOUNT_NAV'},
        windows={'train': None, 'validation': None, 'test': [str(days[0].date()), str(days[-1].date())]},
        walk_forward={'enabled': False},
        costs={'rules': json.loads(json.dumps(manifest['fees'], default=str))})
    tracker.create_experiment('p8-e02', definition)
    save('execution-provenance.json', provenance)
    tracker.append_run('p8-e02', 'p8-e02-run', code=provenance['code'],
        environment=provenance['environment'], command=['run_p8_e02.py'],
        status='SUCCEEDED', qualification='SYNTHETIC', prediction_metrics={},
        portfolio_metrics={key: metric.value for key, metric in evaluated.metrics.items() if metric.value is not None},
        validation={'sample_hash': identity, 'labels': LABELS, 'result_hashes': hashes,
                    'dividend_policy': result.dividend_policy})
    tracker.get_run('p8-e02-run')
    save('account-ledger-reconciliation.json', dict(currency='CNY', absolute_tolerance=1e-8,
        residuals=residuals, sessions=len(result.nav), securities=len(SYMBOLS), events=len(result.entitlements),
        total_paid_gross=str(result.payments.gross.sum()),
        final_cash=float(result.nav.iloc[-1].available_cash),
        final_nav=float(result.nav.iloc[-1].strategy_nav),
        duplicate_entitlements=False, duplicate_payments=False))
    save('verification.json', dict(status='ACCOUNT_EXPERIMENT_PASSED', repeated_run_exact=True,
        no_dividend_regression_exact=True, evaluation_tracker_connected=True,
        starting_git={'head': HEAD, 'tree': TREE, 'status': 'CLEAN'},
        configuration_sha256=identity, result_hashes=hashes,
        network='DENIED_BY_RUNNER', persistent_restart='OUT_OF_SCOPE'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    def deny(*args, **kwargs):
        raise AssertionError('SYNTHETIC acceptance attempted network access')

    socket.socket.connect = deny
    socket.create_connection = deny
    run(args.output)


if __name__ == '__main__':
    main()
