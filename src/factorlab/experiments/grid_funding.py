"""Funding overlays on immutable Paper research exports; no ledger writes."""
import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import sqlite3
from pathlib import Path

D = Decimal
ZERO = D(0)
START = datetime(2026, 5, 14, tzinfo=timezone.utc)
END = datetime(2026, 8, 12, tzinfo=timezone.utc)


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def cashflow(quantity, price, rate):
    """Positive values are receipts; quantity is signed base quantity."""
    return -D(quantity) * D(price) * D(rate)


def settlements(rows, start=START, end=END):
    events = []
    seen = set()
    for row in rows:
        time = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(row['fundingTime']))
        if row['symbol'] != 'NVDAUSDT' or not start <= time < end:
            raise ValueError('funding outside contract/window')
        key = (time, row.get('rateType'))
        if key in seen:
            raise ValueError('duplicate funding settlement')
        seen.add(key)
        price = row.get('markPrice')
        if price is None or D(price) <= 0:
            raise ValueError('settlement mark price missing or nonpositive')
        events.append((time, D(price), D(row['fundingRate']), row.get('rateType')))
    return sorted(events)


def fragment_funding(quantity, buy, sell, events, *, fills_first=False):
    """Attribute a FIFO fragment over its holding interval; exact ties are explicit."""
    return sum((cashflow(quantity, price, rate) for time, price, rate, _ in events
                if (buy <= time < sell if fills_first else buy < time <= sell)), ZERO)


def drawdown(values):
    peak = D(1000)
    worst = ZERO
    for value in values:
        peak = max(peak, value)
        worst = max(worst, (peak-value)/peak)
    return worst * 100


def read_csv(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze_account(directory, events, output, *, end=END):
    trades = read_csv(directory/'trades.csv')
    daily = read_csv(directory/'daily.csv')
    fifo = json.loads((directory/'fifo.json').read_text())
    by_id = {r['event_id']: r for r in trades}
    previous = {}
    connection = sqlite3.connect((directory/'paper.sqlite').resolve().as_uri()+'?mode=ro', uri=True)
    try:
        for row in trades:
            fields = json.loads(connection.execute('SELECT payload FROM events WHERE id=?',
                                                   (row['event_id'],)).fetchone()[0])['fields']
            quantity = D(fields['cumulative_filled_quantity']['decimal'])
            old = previous.get(row['order_id'], ZERO)
            if (fields['client_order_id'] != row['order_id']
                    or timestamp(fields['event_time']['utc']) != timestamp(row['event_time'])
                    or quantity != D(row['cumulative_filled_quantity'])
                    or float(quantity-old) != float(row['executed_shares'])):
                raise ValueError('funding position input differs from authoritative fill')
            row['executed_shares'] = str(quantity-old)
            previous[row['order_id']] = quantity
    finally:
        connection.close()
    lots = {r['event_id']: r for r in fifo['lots']}
    fragments = []
    for match in fifo['matches']:
        lot = lots[match['buy_event_id']]
        fragments.append((D(match['quantity']), timestamp(by_id[match['buy_event_id']]['event_time']),
                          timestamp(by_id[match['sell_event_id']]['event_time']),
                          'risk_exit' if match['purpose'] == 'RISK_EXIT' else 'normal_sell',
                          lot['completed_grid_cycle']))
    for lot in lots.values():
        if D(lot['remaining']):
            fragments.append((D(lot['remaining']), timestamp(by_id[lot['event_id']]['event_time']),
                              end, 'residual', False))
    allocation = dict(normal_sell=ZERO, risk_exit=ZERO, residual=ZERO, completed_cycle=ZERO)
    for quantity, buy, sell, category, completed in fragments:
        amount = fragment_funding(quantity, buy, sell, events)
        allocation[category] += amount
        if completed:
            allocation['completed_cycle'] += amount
    flows = position_settlements(trades, events)
    total = sum((r['funding'] for r in flows), ZERO)
    if sum((allocation[k] for k in ('normal_sell', 'risk_exit', 'residual')), ZERO) != total:
        raise ValueError('FIFO funding allocation differs from position funding')
    result = account_overlay(trades, daily, flows, output)
    return dict(**result, **{'funding_'+k:v for k,v in allocation.items()})


def position_settlements(trades, events):
    """Settle each account independently, with settlement before exact-tie fills."""
    flows = []
    for time, price, rate, kind in events:
        before = sum((D(r['executed_shares']) * (1 if r['side']=='BUY' else -1)
                      for r in trades if timestamp(r['event_time']) < time), ZERO)
        after = before + sum((D(r['executed_shares']) * (1 if r['side']=='BUY' else -1)
                             for r in trades if timestamp(r['event_time']) == time), ZERO)
        flows.append(dict(time=time.isoformat(), quantity=before, rate=rate, mark_price=price,
                          rate_type=kind, funding=cashflow(before, price, rate),
                          tie_alternative=cashflow(after, price, rate)))
    return flows


def account_overlay(trades, daily, flows, output):
    """Overlay funding on saved hourly equity without changing the source account."""
    total = sum((r['funding'] for r in flows), ZERO)
    nav = []
    for row in daily:
        # Export date is bar OPEN; fills/NAV are observed at completed bar end.
        observed = timestamp(row['date']) + timedelta(hours=1) - timedelta(microseconds=1)
        cumulative = sum((r['funding'] for r in flows if timestamp(r['time']) <= observed), ZERO)
        inventory = sum((D(r['executed_shares']) * (1 if r['side']=='BUY' else -1)
                         for r in trades if timestamp(r['event_time']) <= observed), ZERO)
        if float(inventory) != float(row['inventory']):
            raise ValueError('funding position differs from saved hourly inventory')
        original = D(row['equity'])
        nav.append(dict(time=observed.isoformat(), original_nav=original,
                        cumulative_funding=cumulative, research_nav=original+cumulative))
    output.mkdir(parents=True)
    write_csv(output/'nav.csv', nav)
    write_csv(output/'settlements.csv', flows)
    original = D(daily[-1]['equity'])
    # Include any settlements after the last hourly NAV observation but before window end.
    final = original + total
    return dict(original_nav=original, funding=total, research_nav=final,
                original_profit=original-D(1000), adjusted_profit=final-D(1000),
                break_even_observed_funding_multiplier=(original-D(1000))/(-total) if total < 0 else '',
                additional_adverse_funding_to_zero=final-D(1000),
                original_drawdown_pct=drawdown([r['original_nav'] for r in nav]),
                adjusted_hourly_drawdown_pct=drawdown([r['research_nav'] for r in nav]),
                tie_cashflow_difference=sum((r['tie_alternative']-r['funding'] for r in flows), ZERO))


def run(evidence, funding_file, output):
    rows = json.loads(Path(funding_file).read_text())
    events = settlements(rows)
    if not events:
        raise ValueError('no observed funding events; use explicitly labelled sensitivity instead')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for cost in ('base', 'stress'):
        for name in ('cash', 'fixed_budget_buy_hold', 'B/arithmetic', 'B/geometric', 'B/atr',
                     'D/arithmetic', 'D/geometric', 'D/atr'):
            source = Path(evidence)/cost/name
            if name in ('cash', 'fixed_budget_buy_hold'):
                source = source/'arithmetic'
            result = analyze_account(source, events, output/cost/name)
            summaries.append(dict(cost=cost, account=name, **result))
    write_csv(output/'summary.csv', summaries)
    regular = [t for t,_,_,kind in events if kind == 'Regular']
    coverage = dict(status='OBSERVED_HISTORY_RESEARCH_OVERLAY', rows=len(events),
                    first=events[0][0].isoformat(), last=events[-1][0].isoformat(),
                    regular_rows=len(regular), special_rows=sum(k=='Special' for _,_,_,k in events),
                    max_regular_gap_hours=max((b-a).total_seconds()/3600 for a,b in zip(regular, regular[1:])),
                    missing_mark_prices=0, window_start=START.isoformat(), window_end=END.isoformat(),
                    boundaries=['NO LIVE MONEY', 'NOT_EXCHANGE_ACCOUNT_SETTLEMENT'],
                    completeness='Observed API events; no assumed fixed schedule or fabricated events')
    (output/'coverage.json').write_text(json.dumps(coverage, indent=2))
    return summaries
