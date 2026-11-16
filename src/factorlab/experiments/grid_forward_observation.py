"""Normalize bounded R13 shadow observations without changing R12 admission."""
import argparse
from collections import Counter
import csv
from decimal import Decimal as D, InvalidOperation
import hashlib
import json
from pathlib import Path

from factorlab.dataset.identity import json_hash
from factorlab.experiments.grid_forward import utc
from factorlab.experiments.grid_forward_evidence import SCHEMA, evaluate


def read_events(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != ['event']:
            raise ValueError('single event column required')
        return [json.loads(row['event']) for row in reader]


def normalize(events):
    """Preserve the frozen window, all interruptions and original proposal facts."""
    experiments = [e for e in events if e['kind'] == 'experiment']
    if len(experiments) != 1:
        raise ValueError('one independent experiment required')
    experiment = experiments[0]
    parameters = experiment['parameters']
    identity = parameters['experiment_id']
    if parameters['strategy_definition_sha256'] != json_hash(parameters['strategy_definition']):
        raise ValueError('strategy definition digest mismatch')
    bindings = [e for e in events if e['kind'] == 'strategy_binding']
    if len(bindings) != 1:
        raise ValueError('one strategy binding required')
    strategy = bindings[0]
    # Deployment serialization is the existing trading.storage wire format.
    deployment = strategy['deployment_config']
    encoded = json.dumps(deployment, sort_keys=True, separators=(',', ':'))
    if hashlib.sha256(encoded.encode()).hexdigest() != strategy['configuration_hash']:
        raise ValueError('deployment digest mismatch')
    account = dict(deployment['map'])['account_id']
    binding = dict(account=account, instrument=strategy['instrument'],
        strategy=strategy['configuration_hash'], version=strategy['version'],
        interval_seconds=strategy['interval_seconds'],
        session_started_at=experiment['started_at'], prior_bar_ids=[])
    bars, proposals, books, gaps, ends = [], [], [], [], []
    receipts = {}
    for event in events:
        kind = event['kind']
        if kind == 'bar_receipt':
            if event['sha256'] != json_hash(event['raw']):
                raise ValueError('raw Bar digest mismatch')
            receipts[event['sha256']] = event
        elif kind == 'begin':
            receipt = event.get('bar_receipt')
            if receipt is not None:
                raw_receipt = receipts[receipt['sha256']]
                if receipt != {k: v for k, v in raw_receipt.items() if k not in {'kind', 'raw'}}:
                    raise ValueError('Bar receipt identity mismatch')
                raw = raw_receipt['raw']
                if (utc(receipt['opened_at']).timestamp() * 1000 != raw[0]
                        or utc(receipt['closed_at']).timestamp() * 1000 != raw[6] + 1):
                    raise ValueError('raw Bar boundary mismatch')
            bars.append(dict(id=event['source'], opened_at=receipt['opened_at'] if receipt else None,
                closed_at=event['market_at'], received_at=receipt['received_at'] if receipt else event['at'],
                known_at=receipt['known_at'] if receipt else None, decision_at=event['at'],
                completed_bar=receipt['completed_bar'] if receipt else None, live=event.get('live'),
                raw_sha256=receipt['sha256'] if receipt else None))
        elif kind == 'capture':
            for p in event['proposals']:
                if (p['configuration_hash'] != binding['strategy']
                        or p['deployment_config'] != deployment or p['version'] != binding['version']):
                    raise ValueError('proposal strategy binding changed')
                proposals.append(dict(id=p['id'], bar_id=p['source'], market_at=p['market_at'],
                    decision_at=p['proposed_at'], captured_at=p['captured_at'], deadline=p['deadline'],
                    side=p['side'], price=p['limit'], quantity=p['quantity'], account=account,
                    strategy=p['configuration_hash'], version=p['version'], replay=False))
        elif kind == 'book':
            raw = event['raw']
            if (raw['symbol'] != binding['instrument']
                    or utc(event['market_at']).timestamp() * 1000 != raw['time']
                    or D(event['bid']) != D(raw['bidPrice']) or D(event['ask']) != D(raw['askPrice'])
                    or event['bid_quantity'] != raw.get('bidQty')
                    or event['ask_quantity'] != raw.get('askQty')):
                raise ValueError('raw book identity mismatch')
            books.append({k: event[k] for k in
                ('market_at', 'received_at', 'bid', 'ask', 'bid_quantity', 'ask_quantity')})
            if event['bid_quantity'] is None or event['ask_quantity'] is None:
                gaps.append(dict(kind='MISSING_TOP_QUANTITY', at=event['received_at']))
        elif kind == 'end':
            reason = event['reason']
            if reason.startswith('STRATEGY_INVALIDATED:'):
                reason = 'STRATEGY_INVALIDATION'
            ends.append(dict(at=event['at'], ids=event['ids'], reason=reason,
                             original_reason=event['reason']))
        elif kind in {'resume', 'interruption', 'rejected_book'}:
            gaps.append(event)
        elif kind == 'request_result' and event['error'] is not None:
            gaps.append(event)
        elif kind == 'request' and event['method'] != 'GET':
            raise ValueError('non-GET observation prohibited')
        elif kind not in {'binding', 'experiment', 'strategy_binding', 'market_status',
                          'request', 'request_result', 'window_end'}:
            raise ValueError('unknown observation event: ' + kind)
    used_receipts = {b['raw_sha256'] for b in bars}
    binding['prior_bar_ids'] = [f"BINANCE_USDM_DEMO_REST:{binding['interval_seconds']}:{int(utc(r['closed_at']).timestamp())}"
        for digest, r in receipts.items() if digest not in used_receipts]
    window_ends = [e for e in events if e['kind'] == 'window_end']
    if len(window_ends) != 1:
        gaps.append(dict(kind='MISSING_OR_DUPLICATE_WINDOW_END'))
    elif utc(window_ends[0]['at']) < utc(experiment['deadline']):
        gaps.append(dict(kind='EARLY_WINDOW_END', **{k: v for k, v in window_ends[0].items() if k != 'kind'}))
    if sum(e['kind'] == 'request' for e in events) != sum(e['kind'] == 'request_result' for e in events):
        gaps.append(dict(kind='INCOMPLETE_REQUEST_RECEIPT'))
    payload = dict(binding=binding, bars=bars, proposals=proposals, books=books,
                   fills=[], gaps=gaps, ends=ends,
                   observation_id=identity, observation_manifest=experiment, window_ends=window_ends,
                   original_events_sha256=json_hash(events),
                   strategy_definition_sha256=parameters['strategy_definition_sha256'])
    return dict(schema=SCHEMA, kind='FORWARD_SHADOW',
        window=[experiment['started_at'], experiment['deadline']],
        max_quote_gap_seconds=parameters['max_quote_gap_seconds'],
        source=dict(format='NORMALIZED_V1', name=identity, payload=payload, sha256=json_hash(payload)))


def describe(evidence):
    """Describe discrete quotes; these diagnostics do not grant R12 admission."""
    data = evidence['source']['payload']
    observations = []
    pairs = 0
    seen = set()
    for p in data['proposals']:
        if p['id'] in seen:
            continue
        seen.add(p['id'])
        captured = utc(p['captured_at'])
        stop = min(utc(p['deadline']), utc(evidence['window'][1]),
                   *(utc(e['at']) for e in data['ends'] if p['id'] in e['ids']))
        eligible = [b for b in data['books'] if captured < utc(b['market_at']) < stop
                    and captured < utc(b['received_at']) <= stop]
        pairs += len(eligible)
        baseline = next((b for b in reversed(data['books']) if utc(b['market_at']) <= captured
                         and utc(b['received_at']) <= captured), None)
        price, quantity = D(p['price']), D(p['quantity'])
        buy = p['side'] == 'BUY'

        def distance(book):
            return dict(bid_bps=str((price / D(book['bid']) - 1) * 10000),
                        ask_bps=str((price / D(book['ask']) - 1) * 10000))

        touches, crossings, coverage, distances = [], [], [], []
        for b in eligible:
            bid, ask = D(b['bid']), D(b['ask'])
            mid = (bid + ask) / 2
            if mid <= price if buy else mid >= price:
                touches.append(b)
            crossing = ask <= price if buy else bid >= price
            if crossing:
                crossings.append(b)
                top = b['ask_quantity' if buy else 'bid_quantity']
                coverage.append(D(top) >= quantity if top is not None else None)
            distances.append(abs(mid / price - 1) * 10000)
        observations.append(dict(id=p['id'], original_limit=p['price'], side=p['side'],
            quantity=p['quantity'], forward_pairs=len(eligible),
            baseline_distance=distance(baseline) if baseline else None,
            first_forward_distance=distance(eligible[0]) if eligible else None,
            minimum_absolute_midpoint_distance_bps=str(min(distances)) if distances else None,
            minimum_opposite_book_distance_bps=(str(min(
                ((D(b['ask']) - price) if buy else (price - D(b['bid']))) / price * 10000
                for b in eligible)) if eligible else None),
            midpoint_touch=bool(touches), potential_opposite_book_crossing=bool(crossings),
            crossing_top_quantity_coverage=coverage,
            first_touch_delay_seconds=(utc(touches[0]['market_at']) - captured).total_seconds() if touches else None,
            right_censored=not touches, observation_stop=stop.isoformat()))
    return dict(observation_id=data['observation_id'], raw_content_sha256=data['original_events_sha256'],
        counts=dict(observation_windows=1, independent_proposals=len(observations),
            input_proposal_records=len(data['proposals']),
            captured_books=len(data['books']), forward_quote_pairs=pairs,
            touched_proposals=sum(p['midpoint_touch'] for p in observations),
            right_censored_proposals=sum(p['right_censored'] for p in observations)),
        right_censored_ratio=(sum(p['right_censored'] for p in observations) / len(observations)
                              if observations else None),
        descriptive_only=True, proposals=observations, confirmed_exchange_fills=0)


def summarize(windows):
    identities = [w['evidence']['source']['payload']['observation_id'] for w in windows]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate observation window')
    definitions = {w['evidence']['source']['payload']['strategy_definition_sha256'] for w in windows}
    versions = {w['evidence']['source']['payload']['binding']['version'] for w in windows}
    if len(definitions) != 1 or len(versions) != 1:
        raise ValueError('incompatible strategy definitions')
    counts = Counter()
    qualified_books = qualified_pairs = 0
    for window in windows:
        counts.update(window['description']['counts'])
        if window['evaluation']['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION':
            qualified_books += window['description']['counts']['captured_books']
            qualified_pairs += window['description']['counts']['forward_quote_pairs']
    counts.update(qualified_books=qualified_books, qualified_forward_quote_pairs=qualified_pairs)
    return dict(counts=dict(counts), windows=[dict(observation_id=w['description']['observation_id'],
        evidence_hash=w['evaluation']['evidence_hash'],
        engineering_status=w['evaluation']['engineering_status'],
        sample_status=w['evaluation']['sample_status'], issues=w['evaluation']['issues']) for w in windows],
        receipt_status='UNVERIFIED_RECEIPT', economic_status='ECONOMIC_ADVANTAGE_NOT_ESTABLISHED',
        confirmed_exchange_fills=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=Path, nargs='+', required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--evaluated-at', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    record = json.loads(args.protocol.read_text(encoding='utf-8'))
    windows = []
    for path in args.events:
        evidence = normalize(read_events(path))
        # R12 requires numeric quantities. Missing quantities remain absent in the
        # evidence; do not fabricate zeros to turn malformed input into admission.
        try:
            result = evaluate(record, evidence, evaluated_at=args.evaluated_at)
        except InvalidOperation as error:
            if not any(b['bid_quantity'] is None or b['ask_quantity'] is None
                       for b in evidence['source']['payload']['books']):
                raise
            result = dict(engineering_status='INVALID_EVIDENCE', sample_status='INSUFFICIENT_SAMPLE',
                receipt_status='UNVERIFIED_RECEIPT', evidence_hash=json_hash(evidence),
                issues=[dict(code='R12_MALFORMED_INPUT', detail=str(error))],
                engineering_metrics=None, metrics=None,
                financial_conclusion='ECONOMIC_ADVANTAGE_NOT_ESTABLISHED')
        windows.append(dict(evidence=evidence, evaluation=result, description=describe(evidence)))
    aggregate = summarize(windows)
    encoded_windows = [json.dumps(window, indent=2) + '\n' for window in windows]
    encoded_summary = json.dumps(aggregate, indent=2) + '\n'
    for path, window, content in zip(args.events, windows, encoded_windows):
        maximum = window['evidence']['source']['payload']['observation_manifest']['parameters']['max_bytes']
        retained = path.stat().st_size + len(content.encode('utf-8')) + len(encoded_summary.encode('utf-8'))
        capture_summary = path.parent / 'summary.json'
        if capture_summary.exists():
            retained += capture_summary.stat().st_size
        if retained > maximum:
            raise ValueError('normalized evidence exceeds frozen byte budget')
    args.output.mkdir(parents=True, exist_ok=False)
    for index, content in enumerate(encoded_windows, 1):
        (args.output / f'window-{index}.json').write_text(content, encoding='utf-8')
    (args.output / 'summary.json').write_text(encoded_summary, encoding='utf-8')
    print(json.dumps(aggregate))


if __name__ == '__main__':
    main()
