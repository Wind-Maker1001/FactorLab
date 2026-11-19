"""Summarize an F03 shadow window without copying its account binding or journals."""

import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
from math import ceil
from pathlib import Path


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return None
    return dict(count=len(ordered), p50=ordered[ceil(.5 * len(ordered)) - 1],
                p95=ordered[ceil(.95 * len(ordered)) - 1], max=ordered[-1])


def times(rows, key):
    return [datetime.fromisoformat(row[key]) for row in rows]


def gaps(items):
    return [(current - previous).total_seconds() for previous, current in zip(items, items[1:])]


def summarize(path):
    with path.open(encoding='utf-8', newline='') as source:
        events = [json.loads(row['event']) for row in csv.DictReader(source)]
    experiment, = (row for row in events if row['kind'] == 'experiment')
    end, = (row for row in events if row['kind'] == 'window_end')
    books = [row for row in events if row['kind'] == 'book']
    receipts = [row for row in events if row['kind'] == 'bar_receipt']
    live_closes = {row['market_at'] for row in events if row['kind'] == 'public_closed_bar'
                   or (row['kind'] == 'begin' and row['live'])}
    eligible = [row for row in receipts if row['closed_at'] in live_closes]
    interruptions = [dict(at=row['at'], reason=row['reason'])
                     for row in events if row['kind'] == 'interruption']
    results = [row for row in events if row['kind'] == 'request_result']
    methods = Counter(row['method'] for row in results)
    reservations = Counter(row['method'] for row in events if row['kind'] == 'request')
    market_gaps = gaps(times(books, 'market_at'))
    receipt_gaps = gaps(times(books, 'received_at'))
    bar_gaps = gaps(times(eligible, 'closed_at'))
    rounds = []
    for row in (event for event in events if event['kind'] == 'poll_round'):
        operations = {name: data['total_seconds'] for name, data in row['operations'].items()}
        # The wall clock bounds are recorded even when the feed never delivered.
        rounds.append(dict(at=row['started_at'], seconds=row['loop_seconds'], bars=row['bars'],
            books=row['quotes'], reason=row['reason'], error=row['error'], operations=operations,
            account_gets=sum(r['classification'] == 'account_get' for r in row['requests']),
            market_gets=sum(r['classification'] == 'market_get' for r in row['requests']),
            total_requests=row['request_count'], market_at=row['book_market_at'],
            book_received_at=row['book_received_at']))
    names = sorted({key for row in rounds for key in row['operations']})
    operations = {name: distribution(row['operations'][name] for row in rounds
                                    if name in row['operations']) for name in names}
    params = experiment['parameters']
    limits = {key: params[key] for key in ('seconds', 'sample_seconds', 'max_requests', 'max_bytes',
                                          'max_proposals', 'max_quote_gap_seconds')}
    return dict(experiment=params['experiment_id'], mode=params.get('mode', 'SIGNED_GRID_PREVIEW'),
        source_commit=params['version'],
        collector_source_sha256=params['collector_source_sha256'],
        raw_events_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        started_at=experiment['started_at'], deadline=experiment['deadline'], window_end=end['at'],
        end_reason=end['reason'], limits=limits, reserved_methods=dict(reservations),
        requests={method: methods[method] for method in ('GET', 'POST', 'DELETE')},
        http_status=dict(Counter(str(row['http_status']) for row in results)),
        http_errors=dict(Counter(row['error'] for row in results if row['error'] is not None)),
        failed_requests=[dict(endpoint=row['endpoint'], error=row['error'],
                              status=row['http_status'], at=row['completed_at'])
                         for row in results if row['error'] is not None],
        books=len(books), bar_receipts=len(receipts), eligible_closed_bars=len(eligible),
        eligible_closes=[row['closed_at'] for row in eligible],
        book_age_seconds=distribution(row['age_seconds'] for row in books),
        book_market_gap_seconds=distribution(market_gaps),
        book_receipt_gap_seconds=distribution(receipt_gaps),
        eligible_bar_gap_seconds=distribution(bar_gaps),
        market_gaps_over_limit=sum(gap > limits['max_quote_gap_seconds'] for gap in market_gaps),
        receipt_gaps_over_limit=sum(gap > limits['max_quote_gap_seconds'] for gap in receipt_gaps),
        interruptions=interruptions, round_loop_seconds=distribution(row['seconds'] for row in rounds),
        round_account_gets=distribution(row['account_gets'] for row in rounds),
        round_market_gets=distribution(row['market_gets'] for row in rounds),
        round_stage_seconds=operations, rounds=rounds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = summarize(args.events)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
