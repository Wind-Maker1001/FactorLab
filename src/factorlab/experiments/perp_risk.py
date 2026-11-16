"""Reproducible offline MC-04 scenarios and read-only Paper NAV bridge."""
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

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


def paper_frames(directory, *, symbol, mark_prices):
    """Read saved hourly NAV; supplied marks must cover exact completed-bar observations.

    Original equity includes realized/unrealized PnL and all original execution costs.
    Only mark-minus-export-price valuation is added; no trades are replayed here.
    """
    directory = Path(directory)
    with (directory/'daily.csv').open(newline='', encoding='utf-8') as stream:
        daily = list(csv.DictReader(stream))
    frames = []
    for row in daily:
        time = datetime.fromisoformat(row['date'].replace('Z', '+00:00')) + timedelta(hours=1, microseconds=-1)
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
                            D(row['equity']), quantity*(mark-reference)))
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
