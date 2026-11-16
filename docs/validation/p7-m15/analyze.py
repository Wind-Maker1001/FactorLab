"""Reproduce discrete-sampling statistics: python analyze.py RUN_DIRECTORY."""
import csv
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import statistics
import sys


def distribution(values):
    values = sorted(float(v) for v in values)
    return dict(n=len(values), minimum=min(values), median=statistics.median(values),
                maximum=max(values), mean=statistics.mean(values)) if values else dict(n=0)


def analyze(root):
    with (root / 'events.csv').open(newline='', encoding='utf-8') as stream:
        rows = [json.loads(r['event']) for r in csv.DictReader(stream)]
    proposals, baselines, pairs = {}, {}, []
    books = [r for r in rows if r['kind'] == 'book']
    for row in rows:
        if row['kind'] == 'capture':
            proposals.update((p['id'], dict(p)) for p in row['proposals'])
        elif row['kind'] == 'book':
            for update in row['updates']:
                proposals[update['id']].update(update)
                if update['forward']:
                    pairs.append(dict(update, bid=row['bid'], ask=row['ask'], market_at=row['market_at']))
                else:
                    baselines.setdefault(update['id'], dict(update, market_at=row['market_at'],
                        received_at=row['received_at'], age_seconds=row['age_seconds'],
                        bid=row['bid'], ask=row['ask']))
        elif row['kind'] == 'end':
            for key in row['ids']:
                proposals[key].update(reason=row['reason'], ended_at=row['at'],
                    right_censored=proposals[key]['first_touch'] is None)
    count = len(proposals)
    ratio = lambda n: n / count if count else None
    touched = sum(p['first_touch'] is not None for p in proposals.values())
    potential = sum(p['first_potential'] is not None for p in proposals.values())
    censored = sum(p['right_censored'] for p in proposals.values())
    potential_pairs = [u for u in pairs if (
        Decimal(u['ask']) <= Decimal(proposals[u['id']]['limit'])
        if proposals[u['id']]['side'].upper() == 'BUY'
        else Decimal(u['bid']) >= Decimal(proposals[u['id']]['limit']))]
    hours = {}
    for row in books:
        hours.setdefault(str(row['utc_hour']), []).append(row)
    result = dict(conclusion='INSUFFICIENT_EVIDENCE',
        delivery='M15_INSUFFICIENT_VALID_SAMPLES', proposals=count,
        valid_books=len(books), rejected_books=sum(r['kind'] == 'rejected_book' for r in rows),
        forward_pairs=len(pairs), proposals_with_forward_books=len({p['id'] for p in pairs}),
        classifications=dict(NOT_TOUCHED=count-touched, PRICE_TOUCHED=touched,
            POTENTIAL_BOOK_CONDITION=potential, RIGHT_CENSORED=censored),
        touch_fraction=ratio(touched), potential_fraction=ratio(potential), censor_fraction=ratio(censored),
        baseline_proposals=len(baselines), baselines=baselines,
        baseline_bid_distance_bps=distribution(b['distance_bid_bps'] for b in baselines.values()),
        baseline_ask_distance_bps=distribution(b['distance_ask_bps'] for b in baselines.values()),
        spread=distribution(b['spread'] for b in books),
        quote_age_seconds=distribution(b['age_seconds'] for b in books),
        sampling_gap_seconds=distribution((datetime.fromisoformat(b['market_at'])-
            datetime.fromisoformat(a['market_at'])).total_seconds() for a,b in zip(books, books[1:])),
        first_observed_touch_seconds=distribution((datetime.fromisoformat(p['first_touch'])-
            datetime.fromisoformat(p['proposed_at'])).total_seconds()
            for p in proposals.values() if p['first_touch']),
        potential_pairs=len(potential_pairs),
        potential_quantity_covered=sum(p['target_covered_at_top'] is True for p in potential_pairs),
        potential_quantity_unknown=sum(p['target_covered_at_top'] is None for p in potential_pairs),
        utc_hours={h:dict(books=len(bs), spread=distribution(b['spread'] for b in bs)) for h,bs in hours.items()},
        end_reasons={reason:sum(p.get('reason') == reason for p in proposals.values())
                     for reason in sorted({p.get('reason') for p in proposals.values()} - {None})},
        request_reservations=sum(r['kind'] == 'request' for r in rows),
        process_gaps=sum(r['kind'] == 'resume' for r in rows),
        evidence_bytes=(root/'events.csv').stat().st_size)
    (root/'analysis.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    analyze(Path(sys.argv[1]))
