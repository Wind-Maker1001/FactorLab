"""Read-only forward evidence gate. Shadow observations never authorize fills."""
import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path

from factorlab.dataset.identity import json_hash
from factorlab.experiments.grid_forward import (
    CONTROLS, METRICS, REQUIRED, engineering_protocol, utc,
)
from factorlab.experiments.tracker import ExperimentTracker

SCHEMA = 'P7_R12_FORWARD_EVIDENCE_V1'
KINDS = {'HISTORICAL_REFERENCE', 'FORWARD_SHADOW', 'DEMO_CONFIRMED'}
FINANCIAL_REQUIREMENTS = sorted(REQUIRED | {
    'approved_immutable_version', 'approval_receipt', 'original_grid_identity',
    'initial_inventory_and_mark', 'fixed_hold_quantity_before_start',
    'lagged_target_known_at', 'common_termination_at', 'minimum_duration',
    'observed_funding_settlements', 'residual_mark_at_common_end',
    'fifo_complete_cycle_and_risk_exit_attribution', 'inventory_age',
    'external_receipt_verification',
})


def financial_contract():
    """Describe the inactive handoff; deliberately expose no execution switch."""
    return dict(
        active=False, status='FINANCIAL_PROTOCOL_PENDING_APPROVAL',
        required_fields=FINANCIAL_REQUIREMENTS, controls=list(CONTROLS),
        metrics=sorted(METRICS), r09='NEW_SEPARATELY_FROZEN_VERSION_REQUIRED',
        shared=['capital', 'notional_budget', 'fee_and_slippage_schedule',
                'UTC_half_open_window', 'valuation_clock', 'termination_at',
                'economic_failure_conditions'],
        accounting=dict(
            paper='factorlab.experiments.grid_paper.run_experiment',
            fifo='factorlab.experiments.grid_fifo.attribution',
            funding='factorlab.experiments.grid_funding.cashflow / drawdown',
            controls='factorlab.experiments.grid_exposure_benchmark',
            boundary='Future approved adapter must supply result.json, trades.csv, daily.csv '
                     'and authoritative PaperStore fills for FIFO. Paper end filtering is '
                     'inclusive: prefilter bars by [start,end). Historical defaults and '
                     'post-hoc funding overlays are not executable cash',
            residual='Mark remaining FIFO lots at common end; no fabricated liquidation',
            costs='Slippage embedded in fills, not deducted twice',
            cash='Same capital; zero trading, funding and assumed interest',
            fixed_hold='Quantity frozen before start, never fitted to future inventory',
            lagged='Previously known Grid exposure; execution on later observations',
            cycles='Normal SELL exhausts BUY lot; risk exits separately attributed'))


def financial_preflight(candidate):
    """Check a future handoff declaration without approving or running it.

    Each account declares its identical shared terms and its own strategy/fill
    artifact identities. Approval receipts remain independently unverified here.
    """
    missing = sorted(k for k in FINANCIAL_REQUIREMENTS if candidate.get(k) is None)
    accounts = candidate.get('accounts')
    problems = []
    if accounts is None:
        missing.append('accounts')
    elif set(accounts) != set(CONTROLS):
        problems.append('EXACT_FOUR_CONTROLS_REQUIRED')
    else:
        shared = ('capital', 'notional_budget', 'costs', 'start', 'end', 'common_termination_at')
        for name, account in accounts.items():
            if any(account.get(k) != candidate.get(k) for k in shared):
                problems.append('SHARED_TERMS_MISMATCH:' + name)
            if not account.get('artifact_identities'):
                missing.append('accounts.' + name + '.artifact_identities')
        if candidate.get('capital') is not None:
            _number(candidate['capital'])
        if candidate.get('start') is not None and candidate.get('end') is not None:
            if not utc(candidate['start']) < utc(candidate['end']):
                problems.append('INVALID_COMMON_WINDOW')
        if candidate.get('common_termination_at') != candidate.get('end'):
            problems.append('COMMON_END_REQUIRED')
    version = candidate.get('approved_immutable_version')
    if version is not None and version.startswith('r11-'):
        problems.append('R11_CANNOT_BE_ACTIVATED')
    return dict(active=False, structural_status='INCOMPLETE' if missing or problems else 'DECLARED_ONLY',
                missing_fields=missing, problems=problems, receipt_status='UNVERIFIED_RECEIPT',
                financial_status='FINANCIAL_PROTOCOL_PENDING_APPROVAL', metrics=None)


def protocol_parameters(record):
    """Verify a portable tracker envelope without opening or mutating a store."""
    if set(record) != {'schema', 'kind', 'id', 'experiment_id', 'payload', 'sha256'}:
        raise ValueError('invalid R11 envelope')
    if record['sha256'] != json_hash({k: v for k, v in record.items() if k != 'sha256'}):
        raise ValueError('R11 record checksum mismatch')
    protocol = record['payload']['parameters']
    identity = 'r11-' + json_hash(protocol)
    if (record['schema'] != 'FACTORLAB_EXPERIMENT_V1' or record['kind'] != 'experiments'
            or record['id'] != identity or record['experiment_id'] != identity):
        raise ValueError('R11 identity mismatch')
    if protocol != engineering_protocol(protocol['version'], protocol['frozen_at']):
        raise ValueError('immutable R11 engineering protocol changed')
    expected = dict(model=protocol['schema'], parameters=protocol, factors=[],
                    dataset={'admission': 'FROZEN_FORWARD_ONLY'}, label={},
                    windows=dict(train=None, validation=None, test=None),
                    walk_forward={'status': 'BLOCKED_PENDING_FINANCIAL_APPROVAL'}, costs=None)
    if record['payload'] != expected:
        raise ValueError('immutable R11 definition changed')
    return protocol


def m13_data(events):
    """Project published M13 facts, leaving unavailable Bar knowledge explicit."""
    bars, proposals, books, gaps, ends = [], [], [], [], []
    binding = None
    for event in events:
        kind = event['kind']
        if kind == 'binding':
            if binding is not None:
                raise ValueError('duplicate M13 binding')
            binding = event
        elif kind == 'begin':
            bars.append(dict(id=event['source'], closed_at=event['market_at'],
                             decision_at=event['at'], opened_at=None, known_at=None,
                             completed_bar=None, received_at=event['at'], live=None))
        elif kind == 'capture':
            for p in event['proposals']:
                if p['source'] != event['source'] or json_hash(p['deployment_config']) != p['configuration_hash']:
                    raise ValueError('M13 proposal source or configuration digest mismatch')
                deployment = dict(p['deployment_config']['map'])
                proposals.append(dict(id=p['id'], bar_id=p['source'],
                                      decision_at=p['proposed_at'], captured_at=p['captured_at'],
                                      market_at=p['market_at'], deadline=p['deadline'],
                                      side=p['side'], price=p['limit'], quantity=p['quantity'],
                                      strategy=p['configuration_hash'], version=p['version'],
                                      account=deployment['account_id'], replay=False))
        elif kind == 'book':
            books.append({k: event[k] for k in
                          ('market_at', 'received_at', 'bid', 'ask', 'bid_quantity', 'ask_quantity')})
        elif kind == 'end':
            ends.append(dict(at=event['at'], ids=event['ids'], reason=event['reason']))
        elif kind == 'market_status':
            pass
        else:
            # Unknown recovery events cannot silently become uninterrupted observations.
            gaps.append(event)
    if binding is None:
        raise ValueError('M13 binding required')
    return dict(bars=bars, proposals=proposals, books=books, fills=[], gaps=gaps,
                ends=ends, binding=binding)


def read_m13(path, *, start, end, max_quote_gap_seconds):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['event']:
            raise ValueError('M13 single event CSV column required')
        events = [json.loads(row['event']) for row in reader]
    m13_data(events)
    return dict(schema=SCHEMA, kind='FORWARD_SHADOW', window=[start, end],
                max_quote_gap_seconds=max_quote_gap_seconds,
                source=dict(format='M13_EVENTS_V1', name='published M13 events.csv',
                            sha256=json_hash(events), payload=events))


def _number(value, *, zero=False):
    result = Decimal(str(value))
    if not result.is_finite() or (result < 0 if zero else result <= 0):
        raise ValueError('finite positive numeric value required')
    return result


def evaluate(record, evidence, *, evaluated_at):
    """Evaluate declarations and content integrity, not external authenticity.

    Invalid input raises for malformed structure; research admission failures are
    returned as issue codes. No invalid subset is repaired into an admitted sample.
    """
    protocol = protocol_parameters(record)
    if evidence['schema'] != SCHEMA or evidence['kind'] not in KINDS:
        raise ValueError('unsupported evidence schema or category')
    now, frozen = utc(evaluated_at), utc(protocol['frozen_at'])
    start, end = map(utc, evidence['window'])
    if not frozen <= now or not start < end <= now:
        raise ValueError('invalid evaluation window')
    source = evidence['source']
    issues = []

    def issue(code, identity=None):
        issues.append(dict(code=code, identity=identity))

    if not source['name'] or source['sha256'] != json_hash(source['payload']):
        issue('SOURCE_DIGEST_MISMATCH')
    if source['format'] == 'M13_EVENTS_V1':
        if evidence['kind'] != 'FORWARD_SHADOW':
            issue('SHADOW_CANNOT_BECOME_EXECUTION')
        data = m13_data(source['payload'])
        issue('M13_BAR_KNOWLEDGE_NOT_PUBLISHED')
    elif source['format'] == 'NORMALIZED_V1':
        data = source['payload']
    else:
        raise ValueError('unsupported source format')
    if evidence['kind'] == 'HISTORICAL_REFERENCE':
        issue('HISTORICAL_REFERENCE_ONLY')
    max_gap = _number(evidence['max_quote_gap_seconds'])
    binding = data['binding']
    if source['format'] == 'NORMALIZED_V1':
        if not all(binding.get(k) for k in ('account', 'instrument', 'strategy', 'version',
                                           'session_started_at', 'interval_seconds')):
            issue('MISSING_SOURCE_BINDING')
        interval = _number(binding['interval_seconds'])
        session = utc(binding['session_started_at'])
        if not frozen <= session <= now:
            issue('INVALID_TIMELINE', 'session')
        prior_ids = set(binding['prior_bar_ids'])
    else:
        interval, session, prior_ids = None, None, set()
    bars, proposals, books, fills = (data[k] for k in ('bars', 'proposals', 'books', 'fills'))
    summary = evidence.get('summary')
    if summary is not None:
        counts = dict(proposals=len(proposals), books=len(books), confirmed_exchange_fills=len(fills))
        if any(summary.get(k) != count for k, count in counts.items()):
            issue('SOURCE_SUMMARY_MISMATCH')
    if data['gaps']:
        issue('DECLARED_OBSERVATION_GAP')
    bar_by_id, proposal_ids, fill_ids = {}, set(), set()
    previous = None
    for bar in bars:
        identity = bar['id']
        if identity in bar_by_id:
            issue('DUPLICATE_OR_CONFLICTING_BAR', identity)
        bar_by_id[identity] = bar
        if any(bar.get(k) is None for k in ('opened_at', 'known_at', 'completed_bar', 'live')):
            issue('MISSING_BAR_RECEIPT', identity)
            continue
        opened, closed, known, decision, received = (utc(bar[k]) for k in
            ('opened_at', 'closed_at', 'known_at', 'decision_at', 'received_at'))
        if not frozen <= opened < closed <= received <= known <= decision <= now:
            issue('INVALID_TIMELINE', identity)
        if not start <= opened < closed <= end:
            issue('BAR_OUTSIDE_HALF_OPEN_WINDOW', identity)
        if interval is not None and Decimal(str((closed - opened).total_seconds())) != interval:
            issue('BAR_INTERVAL_MISMATCH', identity)
        if identity in prior_ids or (session is not None and opened < session):
            issue('HISTORICAL_REPLAY', identity)
        if bar['completed_bar'] is not True:
            issue('UNCLOSED_BAR', identity)
        if bar['live'] is not True:
            issue('HISTORICAL_REPLAY', identity)
        if previous is not None and opened != previous:
            issue('BAR_GAP_DUPLICATE_OR_ORDER', identity)
        previous = closed
    previous = None
    quote_gaps = 0
    for book in books:
        market, received = (utc(book[k]) for k in ('market_at', 'received_at'))
        if not frozen <= market <= received <= now or not start <= market < end:
            issue('INVALID_TIMELINE', book['market_at'])
        if previous is not None:
            if market <= previous:
                issue('QUOTE_DUPLICATE_OR_ORDER', book['market_at'])
            elif Decimal(str((market - previous).total_seconds())) > max_gap:
                quote_gaps += 1
        previous = market
        bid, ask = _number(book['bid']), _number(book['ask'])
        _number(book['bid_quantity'], zero=True)
        _number(book['ask_quantity'], zero=True)
        if bid > ask:
            issue('CROSSED_QUOTE', book['market_at'])
    if quote_gaps:
        issue('QUOTE_GAP')
    observations, distances, deviations, durations = [], [], [], []
    terminal_ids = set()
    for terminal in data['ends']:
        at = utc(terminal['at'])
        if terminal['reason'] not in {'RUN_END', 'DEADLINE', 'WINDOW_END',
                                      'STRATEGY_INVALIDATION', 'EXPIRED'}:
            issue('TERMINAL_OBSERVATION_INTERRUPTION')
        if not start <= at <= end:
            issue('INVALID_TERMINATION_TIME')
        for identity in terminal['ids']:
            if identity in terminal_ids:
                issue('DUPLICATE_TERMINATION', identity)
            terminal_ids.add(identity)
    previous_capture = None
    for p in proposals:
        identity = p['id']
        if identity in proposal_ids:
            issue('DUPLICATE_OR_CONFLICTING_PROPOSAL', identity)
        proposal_ids.add(identity)
        price, quantity = _number(p['price']), _number(p['quantity'])
        if p['side'] not in {'BUY', 'SELL'}:
            raise ValueError('BUY or SELL required')
        decision, captured, market, deadline = (utc(p[k]) for k in
            ('decision_at', 'captured_at', 'market_at', 'deadline'))
        if not frozen <= market <= decision <= captured <= now or not captured < deadline:
            issue('INVALID_TIMELINE', identity)
        if previous_capture is not None and captured < previous_capture:
            issue('PROPOSAL_ORDER', identity)
        previous_capture = captured
        if not start <= decision < end:
            issue('PROPOSAL_OUTSIDE_HALF_OPEN_WINDOW', identity)
        if p['replay'] is not False:
            issue('HISTORICAL_REPLAY', identity)
        if not all(p[k] for k in ('strategy', 'version', 'account')):
            issue('MISSING_PROPOSAL_BINDING', identity)
        if source['format'] == 'NORMALIZED_V1' and any(
                p[k] != binding[k] for k in ('strategy', 'version', 'account')):
            issue('PROPOSAL_BINDING_CONFLICT', identity)
        if source['format'] == 'M13_EVENTS_V1' and p['version'] != binding['version']:
            issue('PROPOSAL_BINDING_CONFLICT', identity)
        bar = bar_by_id.get(p['bar_id'])
        if bar is None:
            issue('MISSING_DECISION_BAR', identity)
        elif (utc(p['market_at']) != utc(bar['closed_at'])
              or utc(p['decision_at']) != utc(bar['decision_at'])):
            issue('DECISION_BAR_CONFLICT', identity)
        stop = min(deadline, end)
        for terminal in data['ends']:
            if identity in terminal['ids']:
                stop = min(stop, utc(terminal['at']))
        if stop < captured:
            issue('INVALID_TERMINATION_TIME', identity)
        eligible = [b for b in books if captured < utc(b['market_at']) < stop
                    and captured < utc(b['received_at']) <= stop]
        if not eligible:
            issue('MISSING_FORWARD_QUOTE', identity)
        else:
            internal_gaps = sum(
                Decimal(str((utc(current['market_at']) - utc(previous['market_at']))
                            .total_seconds())) > max_gap
                for previous, current in zip(eligible, eligible[1:]))
            if internal_gaps:
                issue('PROPOSAL_QUOTE_GAP', identity)
                quote_gaps += internal_gaps
            edges = [(utc(eligible[0]['market_at']) - captured).total_seconds(),
                     (stop - utc(eligible[-1]['market_at'])).total_seconds()]
            if any(Decimal(str(x)) > max_gap for x in edges):
                issue('QUOTE_EDGE_GAP', identity)
                quote_gaps += sum(Decimal(str(x)) > max_gap for x in edges)
        touched, potential = False, False
        for b in eligible:
            bid, ask = _number(b['bid']), _number(b['ask'])
            mid = (bid + ask) / 2
            touched |= mid <= price if p['side'] == 'BUY' else mid >= price
            potential |= ask <= price if p['side'] == 'BUY' else bid >= price
            distances.append(dict(proposal=identity, market_at=b['market_at'],
                                  bid_bps=str((price / bid - 1) * 10000),
                                  ask_bps=str((price / ask - 1) * 10000)))
            deviations.append(abs(mid / price - 1) * 10000)
        observations.append(dict(id=identity, touched=touched, book_candidate=potential,
                                 right_censored=not touched))
        durations.append(max(0, (stop - captured).total_seconds()))
    if terminal_ids - proposal_ids:
        issue('UNKNOWN_TERMINAL_PROPOSAL')
    if evidence['kind'] != 'DEMO_CONFIRMED' and fills:
        issue('SHADOW_OR_HISTORY_CANNOT_CONTAIN_FILLS')
    if evidence['kind'] == 'DEMO_CONFIRMED' and not fills:
        issue('MISSING_EXCHANGE_CONFIRMATION')
    filled_quantities = {}
    for fill in fills:
        required = ('orderId', 'tradeId', 'source', 'confirmed_at', 'market_at',
                    'quantity', 'price', 'fee', 'fee_asset', 'account', 'proposal_id',
                    'integrity', 'receipt', 'venue', 'endpoint', 'received_at')
        if any(fill.get(k) is None or fill[k] == '' for k in required):
            issue('MISSING_EXCHANGE_CONFIRMATION')
            continue
        key = (fill['account'], fill['tradeId'])
        if key in fill_ids:
            issue('DUPLICATE_OR_CONFLICTING_FILL')
        fill_ids.add(key)
        if (fill['source'] != 'EXCHANGE_CONFIRMED' or fill['integrity'] != 'COMPLETE'
                or fill['venue'] != 'BINANCE_USDM_DEMO'
                or fill['endpoint'] != '/fapi/v1/userTrades'):
            issue('UNVERIFIED_FILL_STRUCTURE')
        p = next((p for p in proposals if p['id'] == fill['proposal_id']), None)
        if p is None or p['account'] != fill['account']:
            issue('FILL_ACCOUNT_OR_PROPOSAL_MISMATCH')
        elif not (utc(p['captured_at']) < utc(fill['market_at'])
                  <= utc(fill['received_at']) <= utc(fill['confirmed_at']) <= now
                  and start <= utc(fill['market_at']) < end):
            issue('INVALID_TIMELINE', fill['tradeId'])
        quantity, price = _number(fill['quantity']), _number(fill['price'])
        _number(fill['fee'], zero=True)
        if fill['receipt'] != json_hash({k: v for k, v in fill.items() if k != 'receipt'}):
            issue('FILL_RECEIPT_DIGEST_MISMATCH')
        if p is not None:
            filled_quantities[p['id']] = filled_quantities.get(p['id'], Decimal(0)) + quantity
            if filled_quantities[p['id']] > _number(p['quantity']):
                issue('FILLED_QUANTITY_EXCEEDS_PROPOSAL', p['id'])
            if ((p['side'] == 'BUY' and price > _number(p['price']))
                    or (p['side'] == 'SELL' and price < _number(p['price']))):
                issue('FILL_LIMIT_VIOLATION', p['id'])
            if utc(fill['market_at']) >= utc(p['deadline']):
                issue('FILL_AFTER_DEADLINE', p['id'])
    valid = not issues
    engineering = None
    if valid:
        covered_seconds = sum((utc(b['closed_at']) - utc(b['opened_at'])).total_seconds()
                              for b in bars)
        engineering = dict(valid_bars=len(bars), valid_forward_proposals=len(proposals),
                           bar_coverage=dict(covered_seconds=covered_seconds,
                                             window_seconds=(end - start).total_seconds(),
                                             ratio=covered_seconds / (end - start).total_seconds()),
                           coverage=dict(numerator=len(proposals), denominator=len(proposals),
                                         ratio=1.0 if proposals else None,
                                         meaning='proposals with complete sampled quote coverage'),
                           interruptions=0, observable_touches=sum(o['touched'] for o in observations),
                           book_candidates=sum(o['book_candidate'] for o in observations),
                           right_censored_ratio=(sum(o['right_censored'] for o in observations)
                                                 / len(observations) if observations else None),
                           duration_seconds=durations, quote_distances=distances,
                           sampled_absolute_midpoint_deviation_bps=(
                               dict(min=str(min(deviations)), max=str(max(deviations)))
                               if deviations else None))
    return dict(schema=SCHEMA, protocol_id=record['id'], protocol_record_hash=record['sha256'],
                evidence_hash=json_hash(evidence), source_hash=source['sha256'],
                evaluated_at=evaluated_at, kind=evidence['kind'],
                engineering_status='VALID_ENGINEERING_OBSERVATION' if valid else (
                    'INVALID_TIMELINE' if any(i['code'] == 'INVALID_TIMELINE' for i in issues)
                    else 'INVALID_EVIDENCE'),
                sample_status='INSUFFICIENT_SAMPLE' if not proposals or not valid else 'NOT_ASSESSED',
                receipt_status='UNVERIFIED_RECEIPT', financial_admission='NOT_FINANCIALLY_ADMITTED',
                financial_status='FINANCIAL_PROTOCOL_PENDING_APPROVAL',
                financial_conclusion='ECONOMIC_ADVANTAGE_NOT_ESTABLISHED', metrics=None,
                engineering_metrics=engineering, issues=issues,
                diagnostics=dict(input_bars=len(bars), input_proposals=len(proposals),
                                 input_books=len(books), quote_gaps=quote_gaps,
                                 declared_interruptions=len(data['gaps'])),
                activation=financial_contract(), safety='NO LIVE MONEY')


def publish(root, record, evidence, result, *, code, command, environment, test_status):
    """Explicitly publish a new R12 study, never update the R11 experiment."""
    expected = evaluate(record, evidence, evaluated_at=result['evaluated_at'])
    if result != expected:
        raise ValueError('result does not match evidence')
    parameters = dict(schema=SCHEMA, protocol_id=record['id'],
                      protocol_hash=record['sha256'], evidence_hash=json_hash(evidence),
                      evaluated_at=result['evaluated_at'], adapter_code=code)
    identity = 'r12-' + json_hash(parameters)
    tracker = ExperimentTracker(root)
    definition = dict(model=SCHEMA, parameters=parameters, factors=[],
                      dataset=dict(source_hash=evidence['source']['sha256'], kind=evidence['kind']),
                      label={}, windows=dict(train=None, validation=None, test=evidence['window']),
                      walk_forward=dict(status='FINANCIAL_PROTOCOL_PENDING_APPROVAL'), costs=None)
    experiment = tracker.create_experiment(identity, definition)
    run = tracker.append_run(identity, identity + '-evaluation', code=code,
                             environment=environment, command=command, status='SUCCEEDED',
                             test_status=test_status, qualification='UNKNOWN',
                             validation=dict(result=result), timestamp=result['evaluated_at'])
    return dict(experiment=experiment, run=run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--evaluated-at', required=True)
    parser.add_argument('--m13', action='store_true')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--max-quote-gap-seconds', type=Decimal)
    args = parser.parse_args()
    record = json.loads(args.protocol.read_text(encoding='utf-8'))
    if args.m13:
        if not all((args.start, args.end, args.max_quote_gap_seconds)):
            parser.error('M13 requires start, end and max-quote-gap-seconds')
        evidence = read_m13(args.evidence, start=args.start, end=args.end,
                            max_quote_gap_seconds=str(args.max_quote_gap_seconds))
    else:
        evidence = json.loads(args.evidence.read_text(encoding='utf-8'))
    print(json.dumps(evaluate(record, evidence, evaluated_at=args.evaluated_at),
                     indent=2, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
