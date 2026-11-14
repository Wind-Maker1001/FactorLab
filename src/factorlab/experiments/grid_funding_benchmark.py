"""Funding attribution on saved R06 executions, with immutable R05 grid references."""
import argparse
from datetime import timedelta
import json
from pathlib import Path

from factorlab.experiments.grid_funding import (
    D, ZERO, account_overlay, drawdown, position_settlements, read_csv,
    settlements, timestamp, write_csv,
)

CONTROLS = ('fixed_012', 'fixed_013', 'lag_B_geometric')
FIELDS = ('original_nav', 'funding', 'research_nav', 'original_profit', 'adjusted_profit',
          'break_even_observed_funding_multiplier', 'additional_adverse_funding_to_zero',
          'original_drawdown_pct', 'adjusted_hourly_drawdown_pct', 'tie_cashflow_difference')


def benchmark_trades(directory):
    trades = read_csv(directory / 'fills.csv')
    for row in trades:
        # R06 simulate executes on the completed bar close, not its OPEN label.
        row['event_time'] = (timestamp(row['execution_date']) + timedelta(hours=1)
                             - timedelta(microseconds=1)).isoformat()
    return trades


def grid_reference(directory, cost):
    row = next(r for r in read_csv(directory / 'summary.csv')
               if r['cost'] == cost and r['account'] == 'B/geometric')
    result = {key: D(row[key]) if row[key] else '' for key in FIELDS}
    nav = read_csv(directory / cost / 'B/geometric/nav.csv')
    flows = read_csv(directory / cost / 'B/geometric/settlements.csv')
    total = sum((D(r['funding']) for r in flows), ZERO)
    if total != result['funding'] or result['original_nav'] + total != result['research_nav']:
        raise ValueError('R05 reference summary differs from saved settlement evidence')
    for point in nav:
        cumulative = sum((D(f['funding']) for f in flows
                          if timestamp(f['time']) <= timestamp(point['time'])), ZERO)
        if (D(point['cumulative_funding']) != cumulative
                or D(point['original_nav']) + cumulative != D(point['research_nav'])):
            raise ValueError('R05 reference hourly NAV does not reconcile')
    if drawdown([D(r['research_nav']) for r in nav]) != result['adjusted_hourly_drawdown_pct']:
        raise ValueError('R05 reference drawdown differs')
    return result


def run(benchmark, funding, grid, output):
    events = settlements(json.loads(funding.read_text()))
    if not events:
        raise ValueError('no observed funding events')
    metrics = read_csv(benchmark / 'metrics.csv')
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for cost in ('base', 'stress'):
        reference = grid_reference(grid, cost)
        expected = D('4.2828937003' if cost == 'base' else '3.9629528345')
        if abs(reference['adjusted_profit'] - expected) > D('0.00000000005'):
            raise ValueError('R05 geometric result differs from known evidence')
        accounts = [dict(cost=cost, account='grid_geometric', **reference)]
        for name in CONTROLS:
            source = benchmark / cost / name
            trades = benchmark_trades(source)
            flows = position_settlements(trades, events)
            result = account_overlay(trades, read_csv(source / 'hourly.csv'), flows,
                                     output / cost / name)
            accounts.append(dict(cost=cost, account=name, **result))
        saved_flows = read_csv(grid / cost / 'B/geometric/settlements.csv')
        saved_events = [(timestamp(r['time']), D(r['mark_price']), D(r['rate']), r['rate_type'])
                        for r in saved_flows]
        if saved_events != events:
            raise ValueError('R05 reference uses different funding events')
        for row in accounts:
            name = 'B_geometric' if row['account'] == 'grid_geometric' else row['account']
            metric = next(m for m in metrics if m['scenario'] == cost and m['name'] == name)
            if abs(D(metric['net_pnl']) - row['original_profit']) > D('0.000000001'):
                raise ValueError('original profit differs from R06 evidence')
            row['rank_before'] = 1 + sum(a['original_profit'] > row['original_profit'] for a in accounts)
            row['rank_after'] = 1 + sum(a['adjusted_profit'] > row['adjusted_profit'] for a in accounts)
            before = reference['original_profit'] - row['original_profit']
            difference = reference['funding'] - row['funding']
            after = reference['adjusted_profit'] - row['adjusted_profit']
            row['grid_minus_account_before'] = before
            row['grid_minus_account_funding'] = difference
            row['grid_minus_account_after'] = after
            row['additional_adverse_funding_to_zero'] = max(ZERO, row['adjusted_profit'])
            row['extra_grid_charge_to_erase_lead'] = after if after > 0 else ''
            row['extra_control_charge_to_erase_lead'] = -after if after < 0 else ''
            multiplier = -before / difference if difference else None
            row['common_funding_multiplier_for_tie'] = multiplier if multiplier is not None and multiplier >= 0 else ''
        rows.extend(accounts)
    write_csv(output / 'summary.csv', rows)
    (output / 'inputs.json').write_text(json.dumps(dict(
        benchmark=str(benchmark), funding=str(funding), grid_reference=str(grid),
        settlement_count=len(events), regular=sum(e[3] == 'Regular' for e in events),
        special=sum(e[3] == 'Special' for e in events),
        fill_time='R06 hour OPEN + 1 hour - 1 microsecond; strict pre-settlement position',
        boundary='NO LIVE MONEY; immutable retrospective funding overlay'), indent=2))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('benchmark', 'funding', 'grid', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    run(args.benchmark, args.funding, args.grid, args.output)


if __name__ == '__main__':
    main()
