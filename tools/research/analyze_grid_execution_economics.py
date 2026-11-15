"""Read-only order-distance and inventory-age attribution of existing Grid exports."""
import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

from factorlab.trading.storage import decode


def analyze(root, window, output):
    nav = pd.read_csv(root / 'daily.csv')
    nav['time'] = pd.to_datetime(nav.date, utc=True) + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)
    prices = nav.set_index('time').close
    trades = pd.read_csv(root / 'trades.csv').set_index('event_id')
    fifo = json.loads((root / 'fifo.json').read_text())
    with sqlite3.connect((root / 'paper.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
        state = decode(json.loads(db.execute('SELECT payload FROM snapshot').fetchone()[0]))
        events = [decode(json.loads(row[0])) for row in db.execute('SELECT payload FROM events')]
    first = {}
    for event in sorted(events, key=lambda event: event.event_time):
        first.setdefault(event.client_order_id, event.event_time)
    end = nav.time.iloc[-1]
    rows = []
    for order_id, record in state.orders.items():
        snapshot = record.snapshot
        intent = snapshot.intent
        start = pd.Timestamp(first[order_id])
        reference = float(prices.loc[start])
        limit = float(intent.limit_price)
        terminal = snapshot.status.value in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED')
        observed = pd.Timestamp(record.observed_at)
        stop = observed if terminal else end
        lifetime = prices.loc[(prices.index > start) & (prices.index <= stop)]
        rows.append(dict(window=window, order_id=order_id, side=intent.side.value,
            submitted=start.isoformat(), status=snapshot.status.value,
            limit=limit, submission_close=reference,
            signed_limit_minus_close_bps=(limit/reference-1)*10000,
            absolute_distance_bps=abs(limit/reference-1)*10000,
            lifetime_hours=(stop-start).total_seconds()/3600,
            right_censored=not terminal, filled_quantity=str(snapshot.cumulative_filled_quantity),
            minimum_observed_close_distance_bps=min((abs(limit/float(p)-1)*10000 for p in lifetime), default=None)))
    pd.DataFrame(rows).to_csv(output / f'{window}-orders.csv', index=False)
    lots = []
    for lot in fifo['lots']:
        buy = pd.Timestamp(trades.loc[lot['event_id'], 'event_time'])
        if float(lot['remaining']):
            lots.append(dict(window=window, buy_time=buy.isoformat(), quantity=lot['remaining'],
                execution_price=lot['price'], age_hours=(end-buy).total_seconds()/3600,
                end_close=float(nav.close.iloc[-1]),
                net_contribution=float(lot['remaining'])*(float(nav.close.iloc[-1])-float(lot['price']))-float(lot['fee'])))
    pd.DataFrame(lots).to_csv(output / f'{window}-residual-lots.csv', index=False)
    result = dict(window=window, orders=len(rows), statuses=pd.Series([r['status'] for r in rows]).value_counts().to_dict(),
        residual_lots=lots)
    frame = pd.DataFrame(rows)
    result['by_status'] = {status: dict(count=len(g), median_distance_bps=float(g.absolute_distance_bps.median()),
        median_lifetime_hours=float(g.lifetime_hours.median()), max_lifetime_hours=float(g.lifetime_hours.max()))
        for status, g in frame.groupby('status')}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', type=Path, required=True)
    parser.add_argument('--new', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = [analyze(args.old, 'R04B', args.output), analyze(args.new, 'R07', args.output)]
    (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
