"""Reproducible offline MC-04 scenarios and read-only Paper NAV bridge."""
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re

from factorlab.trading.contracts import utc, decimal_value

from factorlab.research.perp_risk import D, Frame, Funding, Rules, Tier, evaluate

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def synthetic_rule(initial='.1'):
    return Rules('mc-perp-risk/v1', 'SYNTHUSDT', D(1),
        (Tier(D(0), D(1000000), D(initial), D('.05'), D(0)),),
        START, START+timedelta(days=365), 'MC-04 synthetic arithmetic, not Binance rules',
        D('.0004'), D('.001'))


def price_frames(prices, *, rule, budget, contracts, start=START, step=timedelta(hours=8)):
    prices = tuple(D(str(p)) for p in prices)
    quantity = contracts * rule.contract_unit_base
    return tuple(Frame(start+i*step, (rule.position(contracts, prices[0], price),),
                       budget+quantity*(price-prices[0])) for i, price in enumerate(prices))


def monte_carlo_frames(result, *, path_id, asset_id, rule, budget, contracts, start, step):
    """Consume asset prices, never the Runner's fixed-weight NAV reference."""
    if result.asset_paths is None:
        raise ValueError('MonteCarloRunner save_full_paths=True required')
    return price_frames(result.asset_paths[path_id, :, asset_id], rule=rule, budget=budget,
                        contracts=contracts, start=start, step=step)


def paper_time(value):
    # datetime.fromisoformat silently truncates sub-microsecond precision.
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise ValueError('Paper timestamps require explicit UTC and microsecond precision or coarser')
    time = datetime.fromisoformat(value.replace('Z', '+00:00'))
    utc(time)
    return time


def unique_export_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError('duplicate Paper export field')
        fields[key] = value
    return fields


def paper_execution_times(directory, daily, symbol):
    """Validate a complete hourly grid_paper export, without replaying its costs.

    Cumulative quantities/fees and final orders are Decimal evidence. Float-only
    inventory must match the exact inventory trajectory; ambiguous rounding is
    deliberately unsupported rather than used to infer a pre-fill position.
    """
    required = ('trades.csv', 'result.json', 'manifest.json')
    if any(not (directory/name).is_file() for name in required):
        return None
    try:
        result = json.loads((directory/'result.json').read_text(encoding='utf-8'),
                            object_pairs_hook=unique_export_fields)
        manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'),
                              object_pairs_hook=unique_export_fields)
        with (directory/'trades.csv').open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if (len(fields) != len(set(fields)) or not {
                    'execution_date', 'event_time', 'order_id', 'event_id', 'side',
                    'executed_shares', 'fee', 'cumulative_filled_quantity',
                    'cumulative_fee'}.issubset(fields)):
                raise ValueError('complete unambiguous Paper trade columns required')
            trades = list(reader)
        starts = [paper_time(row['date']) for row in daily]
        if (not starts or any(t.minute or t.second or t.microsecond for t in starts)
                or any(b-a != timedelta(hours=1) for a, b in zip(starts, starts[1:]))
                or result['execution']['interval'] != '1h'
                or result['instrument_id'] != symbol
                or result['funding'] != 'NOT_SIMULATED'
                or manifest['window']['bars'] != len(daily)
                or paper_time(manifest['window']['start']) != starts[0]
                or paper_time(manifest['window']['end']) != starts[-1]
                or result['fills'] != len(trades)):
            raise ValueError('incomplete or mismatched Paper execution export')
        orders = result['orders']
        totals = {key: (D(0), D(0)) for key in orders}
        quantities = {time: D(0) for time in starts}
        fees = {time: D(0) for time in starts}
        events, times = set(), []
        for trade in trades:
            key, identity = trade['order_id'], trade['event_id']
            if not key or not identity or identity in events:
                raise ValueError('missing or duplicate Paper fill identity')
            events.add(identity)
            order = orders[key]
            intent = order['intent']
            start = paper_time(trade['execution_date'])
            time = paper_time(trade['event_time'])
            if (start not in quantities or time != start+timedelta(hours=1, microseconds=-1)
                    or (times and time < times[-1])
                    or intent['client_order_id'] != key or intent['instrument_id'] != symbol
                    or trade['side'] != intent['side'] or trade['side'] not in ('BUY', 'SELL')):
                raise ValueError('Paper fill time or order identity mismatch')
            quantity, fee = D(trade['cumulative_filled_quantity']), D(trade['cumulative_fee'])
            decimal_value(quantity, 'cumulative fill', positive=True)
            decimal_value(fee, 'cumulative fee')
            previous_quantity, previous_fee = totals[key]
            delta, cost = quantity-previous_quantity, fee-previous_fee
            if (delta <= 0 or cost < 0 or quantity > D(intent['quantity'])
                    or D(trade['executed_shares']) != D(str(float(delta)))
                    or D(trade['fee']) != D(str(float(cost)))):
                raise ValueError('inconsistent Paper cumulative execution history')
            totals[key] = quantity, fee
            quantities[start] += delta if trade['side'] == 'BUY' else -delta
            fees[start] += cost
            times.append(time)
        for key, order in orders.items():
            if totals[key] != (D(order['cumulative_filled_quantity']), D(order['cumulative_fee'])):
                raise ValueError('missing Paper execution history for final order')
        inventory = D(str(result['config']['initial_inventory'])) * D(str(result['rules']['quantity_unit']))
        for row, start in zip(daily, starts):
            inventory += quantities[start]
            if D(row['inventory']) != inventory or D(row['fee']) != D(str(float(fees[start]))):
                raise ValueError('Paper inventory or fee history incomplete or insufficiently precise')
        if (D(str(result['final']['inventory'])) != inventory
                or D(str(result['final']['equity'])) != D(daily[-1]['equity'])):
            raise ValueError('Paper final observation mismatch')
        return tuple(times)
    except (KeyError, TypeError, ArithmeticError) as error:
        raise ValueError('incomplete or invalid Paper execution export') from error


def paper_frames(directory, *, symbol, mark_prices):
    """Read saved hourly NAV; supplied marks must cover exact completed-bar observations.

    Original equity includes realized/unrealized PnL and all original execution costs.
    Only mark-minus-export-price valuation is added; no trades are replayed here.
    """
    directory = Path(directory)
    with (directory/'daily.csv').open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError('duplicate Paper daily column')
        daily = list(reader)
    fill_times = paper_execution_times(directory, daily, symbol)
    frames = []
    for row in daily:
        time = paper_time(row['date']) + timedelta(hours=1, microseconds=-1)
        mark = D(str(mark_prices[time]))
        quantity = D(row['inventory'])
        reference = D(row['close'])
        # Entry is supplied by the existing exported unrealized PnL, not inferred trades.
        unrealized = D(row['unrealized'])
        if not quantity and unrealized:
            raise ValueError('nonzero unrealized PnL with zero inventory')
        entry = reference-unrealized/quantity if quantity else reference
        from factorlab.trading.contracts import Position
        frames.append(Frame(time, (Position(symbol, quantity, entry, mark, 'USDT'),),
                            D(row['equity']), quantity*(mark-reference), fill_times, symbol))
    return tuple(frames)


def run(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    specifications = [
        ('unlevered', '1', '1000', '10', ['100','100','90'], [], 'CROSS', None),
        ('positive_funding', '.1', '1000', '10', ['100','100','100'], ['.01','.01'], 'CROSS', None),
        ('negative_funding', '.1', '1000', '10', ['100','100','100'], ['-.01','-.01'], 'CROSS', None),
        ('mark_crash', '.1', '1000', '50', ['100','85','79'], ['.01','.01'], 'CROSS', None),
        ('isolated_crash', '.1', '1000', '10', ['100','90','80'], [], 'ISOLATED', '100'),
        ('same_exposure_small_budget', '.1', '200', '10', ['100','90','80'], [], 'CROSS', None),
        ('short_positive', '.1', '1000', '-10', ['100','100','100'], ['.01','.01'], 'CROSS', None),
    ]
    results, inputs = {}, {}
    for name, rate, capital, quantity, prices, rates, mode, isolated in specifications:
        rule = synthetic_rule(rate)
        frames = price_frames(prices, rule=rule, budget=D(capital), contracts=D(quantity))
        funding = tuple(Funding(f'{name}-{i}', frames[i].time, rule.symbol, D(r),
            frames[i].positions[0].mark_price, 'ASSUMED_SETTLEMENT', 'independent frozen funding scenario')
            for i, r in enumerate(rates, 1))
        result = evaluate(frames=frames, rules={rule.symbol: rule}, funding=funding,
            mode=mode, budget=D(capital), order_margin=D(0), portfolio_complete=True,
            equity_basis='SYNTHETIC_WALLET', isolated_collateral=D(isolated) if isolated else None)
        inputs[name] = dict(rule=asdict(rule), frames=[asdict(f) for f in frames],
            funding=[asdict(f) for f in funding], mode=mode, budget=capital,
            isolated_collateral=isolated, portfolio_complete=True, order_margin='0')
        results[name] = result
    for name, data in [('inputs.json', inputs), ('results.json', results)]:
        (output/name).write_text(json.dumps(data, default=encode, indent=2)+'\n', encoding='utf-8')
    for name, result in results.items():
        with (output/f'{name}.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(result['rows'][0]))
            writer.writeheader()
            writer.writerows(result['rows'])
    return results
