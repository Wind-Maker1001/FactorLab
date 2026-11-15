"""Immutable forward-study engineering contracts; no live trading authorization."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from factorlab.dataset.identity import json_hash
from factorlab.experiments.tracker import ExperimentTracker

SCHEMA = 'P7_R11_ENGINEERING_V1'
CONTROLS = ('grid_original', 'fixed_hold', 'lagged_exposure', 'cash')
REQUIRED = {
    'strategy_commit', 'strategy_parameters', 'risk_version', 'data_source',
    'interval', 'start', 'end', 'capital', 'notional_budget', 'costs',
    'order_timeout', 'risk_exit', 'accounting', 'benchmark_parameters',
    'primary_metric', 'failure_conditions', 'minimum_bars', 'missing_data_policy',
}
METRICS = {
    'terminal_nav', 'net_profit', 'max_drawdown', 'funding_adjusted_profit',
    'mean_notional', 'max_notional', 'realized_pnl', 'unrealized_pnl',
    'inventory_age_hours', 'residual_inventory', 'complete_cycles',
    'cycle_net_contribution', 'fees', 'slippage', 'funding', 'max_committed',
}


def utc(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError('explicit UTC timestamp required')
    return result


def engineering_protocol(version, frozen_at):
    utc(frozen_at)
    if not version:
        raise ValueError('version required')
    return dict(schema=SCHEMA, version=version, frozen_at=frozen_at,
                financial_status='PENDING_APPROVAL', financial_parameters=None,
                controls=list(CONTROLS), r09='EXCLUDED_UNTIL_SEPARATELY_FROZEN',
                historical_windows=[['2026-05-14', '2026-08-12'],
                                    ['2026-08-12', '2026-11-10']],
                evidence_classes=['HISTORICAL_REFERENCE', 'FORWARD_PAPER', 'DEMO_CONFIRMED'],
                knowledge_rule='closed_at <= known_at <= decision_at <= evaluated_at',
                execution_rule='orders generated at a close execute only on later observations',
                funding_rule='observed settlements only; post-hoc overlay is not executable cash',
                comparison_contract=dict(
                    capital='one initial capital and one predeclared notional cap for all accounts',
                    costs='one fee/slippage schedule and settlement evidence for all accounts',
                    window='identical UTC half-open [start,end), mark and observation clock',
                    fixed_hold='quantity frozen before start, never fitted to future mean inventory',
                    lagged_exposure='prior observed Grid target only; execute on later observation',
                    cash='same initial capital, zero trading/funding and zero assumed interest',
                    attribution='Paper simulation, statistical inference and Demo execution are distinct'),
                accounting_contract=dict(
                    nav='initial capital + realized PnL + unrealized PnL - fees; slippage in fill price',
                    funding_nav='NAV + cumulative signed observed funding cashflows',
                    net_profit='terminal NAV minus initial capital',
                    drawdown='max(1 - NAV / running peak), including initial capital',
                    exposure='marked signed position and absolute notional at each observation',
                    inventory='FIFO lots, age at common end, residual inventory marked not force-filled',
                    cycles='entire BUY lot exhausted by normal SELL, excluding risk-exit contamination',
                    costs='fees plus separately reported embedded slippage; never deduct slippage twice',
                    differences='Grid minus each preregistered control on funding-adjusted account profit'),
                activation_rule='new immutable financial version; never mutate this engineering version',
                missing_data_rule='BLOCK; do not interpolate or silently shorten window',
                conclusion='NOT_ESTABLISHED', safety='NO LIVE MONEY')


def freeze(root, protocol):
    if protocol['schema'] != SCHEMA or protocol['financial_status'] != 'PENDING_APPROVAL':
        raise ValueError('this engineering release cannot activate financial rules')
    if protocol != engineering_protocol(protocol['version'], protocol['frozen_at']):
        raise ValueError('unsupported engineering protocol')
    identity = 'r11-' + json_hash(protocol)
    definition = dict(model=SCHEMA, parameters=protocol, factors=[],
                      dataset={'admission': 'FROZEN_FORWARD_ONLY'}, label={},
                      windows=dict(train=None, validation=None, test=None),
                      walk_forward={'status': 'BLOCKED_PENDING_FINANCIAL_APPROVAL'}, costs=None)
    return ExperimentTracker(root).create_experiment(identity, definition)


def admit(protocol, evidence, *, evaluated_at):
    """Validate supplied timing receipts, never attest to their external authenticity."""
    now = utc(evaluated_at)
    frozen = utc(protocol['frozen_at'])
    if frozen > now:
        raise ValueError('freeze lies in the future')
    kind = evidence['kind']
    if kind not in {'FORWARD_PAPER', 'DEMO_CONFIRMED'}:
        raise ValueError('historical or shadow evidence is not forward execution evidence')
    rows = evidence['observations']
    previous = None
    for row in rows:
        opened, closed, known, decision = (utc(row[k]) for k in
                                         ('opened_at', 'closed_at', 'known_at', 'decision_at'))
        if not frozen <= opened < closed <= known <= decision <= now:
            raise ValueError('non-forward, unclosed or future information')
        if not isinstance(row['receipt'], str) or not row['receipt'] or row['completed_bar'] is not True:
            raise ValueError('closed bar and knowledge receipt required')
        if previous is not None and opened != previous:
            raise ValueError('missing, duplicate or unordered observations')
        previous = closed
        if kind == 'DEMO_CONFIRMED':
            if not row.get('exchange_trade_id') or not row.get('exchange_order_id'):
                raise ValueError('exchange confirmed trade and order IDs required')
            if row.get('fill_source') != 'EXCHANGE_CONFIRMED':
                raise ValueError('shadow touches are not exchange fills')
    return len(rows)


def evaluate(root, identity, evidence, *, evaluated_at):
    """Engineering gate. Financial activation requires a new approved protocol version."""
    # TODO(R11 activation): bind approved financial rules to Paper/FIFO/funding adapters
    # and verify external receipt content before any financial sample qualification.
    record = ExperimentTracker(root).get_experiment(identity)
    protocol = record['payload']['parameters']
    if identity != 'r11-' + json_hash(protocol):
        raise ValueError('protocol identity mismatch')
    count = admit(protocol, evidence, evaluated_at=evaluated_at)
    return dict(schema=SCHEMA, protocol_id=identity, protocol_record_hash=record['sha256'],
                evidence_hash=json_hash(evidence), evaluated_at=evaluated_at,
                status='BLOCKED',
                sample_status='NOT_ASSESSED' if count else 'INSUFFICIENT_FORWARD_SAMPLE',
                reason='FINANCIAL_PROTOCOL_PENDING_APPROVAL', timing_validated_observations=count,
                evidence_qualification='UNVERIFIED_RECEIPTS',
                financial_conclusion='NOT_ESTABLISHED', metrics=None,
                required_financial_fields=sorted(REQUIRED), required_metrics=sorted(METRICS),
                controls=protocol['controls'], safety='NO LIVE MONEY')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('freeze')
    create.add_argument('--store', required=True)
    create.add_argument('--version', required=True)
    create.add_argument('--frozen-at', required=True)
    evaluate_parser = sub.add_parser('evaluate')
    evaluate_parser.add_argument('--store', required=True)
    evaluate_parser.add_argument('--identity', required=True)
    evaluate_parser.add_argument('--evidence', type=Path, required=True)
    evaluate_parser.add_argument('--evaluated-at', required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        result = freeze(args.store, engineering_protocol(args.version, args.frozen_at))
    else:
        result = evaluate(args.store, args.identity,
                          json.loads(args.evidence.read_text(encoding='utf-8')),
                          evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
