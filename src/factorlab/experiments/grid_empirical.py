"""Bounded fixed-parameter hourly study using the unified Paper research runner."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from time import perf_counter

import pandas as pd

from factorlab.experiments.grid_paper import D, _write, run_experiment
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market
from factorlab.trading.storage import PaperStore


ARMS = ('no-trade', 'buy-hold', 'arithmetic', 'geometric', 'atr')
COSTS = {'low': (0, 0), 'base': (4, 6), 'stress': (8, 12)}


def load_hourly(snapshot):
    """Read only the projected fixed snapshot; reject corrupt OHLC and gaps."""
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['interval'] != '1h' or not manifest['integrity']['complete']:
        raise ValueError('complete hourly snapshot required')
    path = snapshot / manifest['tables']['bars']['path']
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != manifest['tables']['bars']['sha256']:
        raise ValueError('snapshot bars digest mismatch')
    frame = pd.read_parquet(path, columns=['open_time', 'close_time', 'open', 'high', 'low', 'close', 'volume'])
    times = frame.open_time
    if (str(times.dt.tz) != 'UTC' or times.duplicated().any() or not times.is_monotonic_increasing
            or not times.eq(times.dt.floor('h')).all()):
        raise ValueError('unique ordered UTC hourly timestamps required')
    expected = pd.date_range(pd.to_datetime(manifest['range']['start_ms'], unit='ms', utc=True),
                             pd.to_datetime(manifest['range']['end_ms'], unit='ms', utc=True),
                             freq='h', inclusive='left')
    if list(times) != list(expected):
        raise ValueError('missing hourly bars; no filling permitted')
    if not frame.close_time.eq(times + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1)).all():
        raise ValueError('invalid completed hour close time')
    for row in frame.itertuples():
        o, h, l, c, v = map(D, (row.open, row.high, row.low, row.close, row.volume))
        if not all(x.is_finite() for x in (o, h, l, c, v)) or not (0 < l <= min(o, c) <= max(o, c) <= h) or v < 0:
            raise ValueError('invalid hourly OHLC or volume')
    bars = [dict(id=f'hour-{i}', time=row.open_time.isoformat(), price=float(row.close),
                 high=float(row.high), low=float(row.low), close=float(row.close),
                 volume=int(D(row.volume)), completed_bar=True)
            for i, row in enumerate(frame.itertuples())]
    receipts = [r for r in manifest['raw_receipts'] if r['endpoint'] == 'klines' and r['params'].get('interval') == '1h']
    identity = dict(data_version=manifest['data_version'], bars_sha256=digest,
        acquired_at=manifest['acquired_at'], instrument_id=manifest['contract']['instrument_id'],
        source='BINANCE_USDM_PUBLIC_REST', interval='1h', rows=len(frame),
        start=times.iloc[0].isoformat(),
        end_exclusive=(times.iloc[-1] + pd.Timedelta(hours=1)).isoformat(),
        last_close=frame.close_time.iloc[-1].isoformat(),
        missing_bars=0, duplicate_timestamps=0, invalid_ohlcv=0,
        zero_volume_bars=int(frame.volume.map(D).eq(0).sum()),
        flat_bars=int(frame.high.eq(frame.low).sum()),
        raw_receipts=[{k: r[k] for k in ('url', 'sha256', 'acquired_at')} for r in receipts],
        liquidity='observed base volume floored to integer units before 1% participation',
        valuation='trade close, not historical intrahour mark; synthetic close execution',
        funding='NOT_IN_PAPER_LEDGER')
    return bars, identity


def summarize(directory, capital=1000):
    directory = Path(directory)
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    nav = pd.read_csv(directory / 'daily.csv')
    trades = pd.read_csv(directory / 'trades.csv')
    store = PaperStore(directory / 'paper.sqlite')
    try:
        state = store.load()
        events = [event for _, event in store.order_events() if event.fill_price is not None]
        event_ids = {e.event_id for e in events}
        if set(trades.event_id) != event_ids:
            raise ValueError('fill log differs from authoritative Paper events')
        fees = sum((r.snapshot.cumulative_fee for r in state.orders.values()), D(0))
        position = state.positions.get(result['instrument_id'])
        unrealized = position.quantity * (position.mark_price-position.entry_price) if position else D(0)
        equity = state.cash['USDT'] + unrealized
        if abs(equity - D(nav.equity.iloc[-1])) > D('0.00000001'):
            raise ValueError('NAV differs from Paper account')
        if abs(fees - D(trades.fee.sum())) > D('0.00000001'):
            raise ValueError('fees differ from authoritative orders')
        previous = {}
        notional = D(0)
        for event in events:
            old = previous.get(event.client_order_id, D(0))
            notional += (event.cumulative_filled_quantity-old) * event.fill_price
            previous[event.client_order_id] = event.cumulative_filled_quantity
        if abs(notional-D(trades.trade_notional.sum())) > D('0.00000001'):
            raise ValueError('turnover differs from authoritative events')
        orders = [r.snapshot for r in state.orders.values()]
        partials = sum(e.status.value == 'PARTIALLY_FILLED' for e in events)
    finally:
        store.db.close()
    slip = trades.slippage.sum()
    net = float(equity)-capital
    gross = net + float(fees) + slip
    returns = nav.equity.pct_change().fillna(nav.equity.iloc[0]/capital-1)
    return dict(initial_equity=capital, final_equity=float(equity), net_return=net/capital,
        gross_pnl_cost_addback=gross, net_pnl=net, realized_gross=nav.realized_gross.iloc[-1],
        unrealized=float(unrealized), fees=float(fees), slippage=slip,
        total_transaction_cost=float(fees)+slip,
        cost_to_gross= (float(fees)+slip)/gross if gross > 0 else None,
        max_drawdown=nav.drawdown.max(), max_committed=nav.committed.max(),
        max_inventory=nav.inventory.abs().max(), max_net_exposure=nav.net_exposure.abs().max(),
        max_period_loss=max(0, -returns.min()), risk_events=len(result['risk']),
        risk_triggers=sum(r['kind']=='RISK_TRIGGER' for r in result['risk']),
        orders=len(orders), fills=len(trades), buy_fills=int(trades.side.eq('BUY').sum()),
        sell_fills=int(trades.side.eq('SELL').sum()), partial_fill_events=partials,
        cancellations=sum(o.status.value=='CANCELED' for o in orders),
        traded_notional=float(notional), turnover=float(notional)/capital,
        mean_inventory=nav.inventory.mean(), mean_net_exposure=nav.net_exposure.mean(),
        end_inventory=nav.inventory.iloc[-1], exposure_fraction=nav.inventory.ne(0).mean(),
        funding='NOT_IN_PAPER_LEDGER', ledger_reconciled=True)


def funding_sensitivity(directory, funding):
    """Conditional cost attribution, not a Paper settlement or funded-strategy rerun."""
    nav = pd.read_csv(Path(directory)/'daily.csv')
    # Positions change only at the simulated close timestamp, never at bar open.
    times = pd.to_datetime(nav.date, utc=True) + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)
    holdings = pd.DataFrame({'time': times, 'quantity': nav.inventory})
    matched = pd.merge_asof(funding.sort_values('funding_time'), holdings,
                            left_on='funding_time', right_on='time', direction='backward')
    matched.quantity = matched.quantity.fillna(0)
    costs = matched.quantity * matched.funding_rate.map(float) * matched.mark_price.map(float)
    regular = float(costs[matched.rate_type.eq('Regular')].sum())
    special = float(costs[~matched.rate_type.eq('Regular')].sum())
    return dict(regular_linear_cost=regular, special_linear_cost=special,
                all_events_linear_cost=regular+special,
                interpretation='conditional quantity*event_mark*rate; not Paper-booked; Special rules unverified')


def run_study(output, *, snapshot, funding_snapshot, config_path, rules_path):
    started = perf_counter()
    bars, identity = load_hourly(snapshot)
    config = GridConfig(**json.loads(Path(config_path).read_text(encoding='utf-8')))
    rules = GridRules(**{k: D(v) for k, v in json.loads(Path(rules_path).read_text(encoding='utf-8')).items()})
    funding_snapshot = Path(funding_snapshot)
    fm = json.loads((funding_snapshot/'manifest.json').read_text(encoding='utf-8'))
    funding = pd.read_parquet(funding_snapshot/fm['tables']['funding']['path'],
                             columns=['funding_time', 'funding_rate', 'mark_price', 'rate_type'])
    begin = pd.Timestamp(bars[0]['time'])
    end = pd.Timestamp(bars[-1]['time']) + pd.Timedelta(hours=1)
    funding = funding.loc[(funding.funding_time >= begin) & (funding.funding_time < end)]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _write(output/'data-identity.json', identity | dict(funding_version=fm['data_version'],
        funding_events=len(funding), funding_types=funding.rate_type.value_counts().to_dict()))
    rows = []
    windows = [('full', bars)] + [(name, bars[i*len(bars)//3:(i+1)*len(bars)//3])
                                 for i, name in enumerate(('early', 'middle', 'late'))]
    for window, sample in windows:
        for scenario, (fee, slip) in COSTS.items():
            if window != 'full' and scenario != 'base':
                continue
            for arm in ARMS:
                directory = output/window/scenario/arm
                settings = replace(config, mode=arm if arm in ARMS[2:] else 'arithmetic',
                                   fee_bps=fee, slippage_bps=slip)
                run_experiment(directory, bars=sample, config=settings, rules=rules,
                    capital=1000, fee_rate=D(fee)/10000, slippage=D(slip)/10000,
                    participation=D('.01'), interval='1h', market=Market.BINANCE_USDM,
                    instrument_id=identity['instrument_id'], data_identity=identity,
                    no_trade=arm=='no-trade', buy_hold=arm=='buy-hold')
                metrics = summarize(directory)
                fs = funding.loc[(funding.funding_time >= pd.Timestamp(sample[0]['time'])) &
                    (funding.funding_time < pd.Timestamp(sample[-1]['time'])+pd.Timedelta(hours=1))]
                sensitivity = funding_sensitivity(directory, fs)
                _write(directory/'core.json', metrics | dict(funding_sensitivity=sensitivity))
                rows.append(dict(window=window, scenario=scenario, arm=arm, **metrics,
                                 **{k:v for k,v in sensitivity.items() if k!='interpretation'}))
                print(f'{window}/{scenario}/{arm}: {metrics["final_equity"]:.9f}', flush=True)
    table = pd.DataFrame(rows)
    table.to_csv(output/'summary.csv', index=False)
    _write(output/'study.json', dict(status='P7_R02_EMPIRICAL_BASELINE_COMPLETED',
        wall_seconds=perf_counter()-started, runs=len(rows), parameters_selected=False,
        boundaries=['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']))
    return table
