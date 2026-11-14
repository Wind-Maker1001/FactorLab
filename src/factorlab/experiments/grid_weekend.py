"""Weekend attribution of frozen hourly research exports, without account execution."""
import argparse
import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly
from factorlab.trading.storage import decode

# NYSE full-day closures within the fixed May 14--November 10, 2026 window.
HOLIDAYS = {'2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07'}


def labels(times):
    times = pd.DatetimeIndex(times)
    local = times.tz_convert('America/New_York')
    hours = local.hour + local.minute / 60
    business = (local.dayofweek < 5) & ~np.isin(local.strftime('%Y-%m-%d'), list(HOLIDAYS))
    session = np.where(local.dayofweek >= 5, 'US_weekend_closed',
              np.where(~business, 'US_holiday_closed',
              np.where((hours >= 10) & (hours < 16), 'US_regular_full_hour',
              np.where((hours >= 9) & (hours < 10), 'US_open_mixed_hour', 'US_weekday_extended'))))
    return pd.DataFrame({'time': times, 'utc_day': np.where(times.dayofweek == 5, 'Saturday',
        np.where(times.dayofweek == 6, 'Sunday', 'weekday')), 'us_session': session})


def weekend_key(times):
    times = pd.DatetimeIndex(times)
    return times.normalize() - pd.to_timedelta((times.dayofweek - 5) % 7, unit='D')


def binary_model(cost=0.):
    gain, loss = .05-cost, -.70-cost
    return {'cost_per_period': cost, 'gain': gain, 'loss': loss,
            'arithmetic_break_even_p': -loss/(gain-loss),
            'geometric_break_even_p': -math.log1p(loss)/(math.log1p(gain)-math.log1p(loss))}


def describe(frame):
    r = frame.log_return.dropna()
    consecutive = frame.time.diff().eq(pd.Timedelta(hours=1)) & frame.log_return.notna() & frame.log_return.shift().notna()
    continuation = np.sign(frame.log_return).eq(np.sign(frame.log_return.shift()))[consecutive]
    return dict(hours=len(frame), return_observations=len(r), mean_log_return=r.mean(),
        std_log_return=r.std(), q01=r.quantile(.01), q05=r.quantile(.05),
        median=r.median(), q95=r.quantile(.95), q99=r.quantile(.99),
        min_log_return=r.min(), max_log_return=r.max(), realized_vol=np.sqrt(np.sum(r*r)),
        mean_hourly_range=frame.range_fraction.mean(), max_hourly_range=frame.range_fraction.max(),
        extreme_abs_1pct=float((r.abs() >= .01).mean()),
        extreme_abs_3pct=float((r.abs() >= .03).mean()),
        continuation=float(continuation.mean()))


def market(bars):
    frame = pd.DataFrame(bars)
    frame['time'] = pd.to_datetime(frame.time, utc=True)
    frame = frame.sort_values('time').reset_index(drop=True)
    frame['log_return'] = np.log(frame.close/frame.close.shift())
    frame['range_fraction'] = (frame.high-frame.low)/frame.close.shift()
    frame = frame.merge(labels(frame.time), on='time', validate='one_to_one')
    frame['break_up'] = frame.close > frame.high.shift().rolling(24).max()
    frame['break_down'] = frame.close < frame.low.shift().rolling(24).min()
    frame['break_eligible'] = frame.index >= 24
    summaries = []
    for field in ('utc_day', 'us_session'):
        for name, g in frame.groupby(field):
            eligible = g[g.break_eligible]
            summaries.append(dict(partition=field, group=name, **describe(g),
                breakout_24h_frequency=float((eligible.break_up | eligible.break_down).mean())))
    daily = []
    for day, g in frame.groupby(frame.time.dt.floor('D')):
        r = g.log_return.dropna()
        prior = frame.loc[frame.time == day-pd.Timedelta(hours=1), 'close']
        anchor = prior.iloc[0] if len(prior) else np.nan
        daily.append(dict(day=day, utc_day=g.utc_day.iloc[0], hours=len(g),
            max_adverse_long=g.low.min()/anchor-1, max_favorable_long=g.high.max()/anchor-1,
            realized_vol=np.sqrt(np.sum(r*r)), log_return=r.sum(),
            range_fraction=(g.high.max()-g.low.min())/g.close.iloc[0],
            trend_efficiency=abs(r.sum())/r.abs().sum() if r.abs().sum() else 0))
    weekends = []
    subset = frame[frame.utc_day != 'weekday']
    for start, g in subset.groupby(weekend_key(subset.time)):
        prior = frame.loc[frame.time == start-pd.Timedelta(hours=1), 'close']
        complete = len(g)==48 and len(prior)==1
        anchor = prior.iloc[0] if len(prior) else np.nan
        r = g.log_return.dropna()
        weekends.append(dict(start=start, hours=len(g), complete=complete,
            close_return=g.close.iloc[-1]/anchor-1, max_adverse_long=g.low.min()/anchor-1,
            max_favorable_long=g.high.max()/anchor-1,
            realized_vol=np.sqrt(np.sum(r*r)),
            trend_efficiency=abs(r.sum())/r.abs().sum() if r.abs().sum() else 0,
            **{'range_fraction': (g.high.max()-g.low.min())/anchor}))
    return pd.DataFrame(summaries), pd.DataFrame(daily), pd.DataFrame(weekends)


def account(source, funding):
    nav = pd.read_csv(source/'daily.csv')
    trades = pd.read_csv(source/'trades.csv')
    fifo = json.loads((source/'fifo.json').read_text())
    times = pd.to_datetime(nav.date, utc=True)
    out = labels(times)
    out['pnl'] = nav.equity.diff().fillna(nav.equity.iloc[0]-1000).to_numpy()
    out['price_carry'] = (nav.inventory.shift().fillna(0)*nav.close.diff().fillna(0)).to_numpy()
    out['fee'] = nav.fee.to_numpy()
    out['slippage'] = nav.slippage.to_numpy()
    out['execution_reference'] = out.pnl-out.price_carry+out.fee+out.slippage
    out['inventory'] = nav.inventory.to_numpy()
    out['notional'] = (nav.inventory.abs()*nav.close).to_numpy()
    out['unrealized_change'] = nav.unrealized.diff().fillna(nav.unrealized.iloc[0]).to_numpy()
    out['paused'] = nav.state.eq('PAUSED').to_numpy()
    out['exited'] = nav.state.eq('EXITED').to_numpy()
    flow = pd.read_csv(funding/'settlements.csv')
    flow['time'] = pd.to_datetime(flow.time, utc=True, format='ISO8601').dt.floor('h')
    flows = flow.groupby('time').funding.sum()
    out['funding'] = out.time.map(flows).fillna(0)
    out['adjusted_pnl'] = out.pnl+out.funding
    out['equity_before'] = 1000+out.adjusted_pnl.cumsum()-out.adjusted_pnl
    out['adjusted_equity'] = 1000+out.adjusted_pnl.cumsum()
    out['closed_net_at_sale'] = 0.
    out['risk_exit_net_at_sale'] = 0.
    by_id = trades.set_index('event_id')
    completed = {lot['event_id'] for lot in fifo['lots'] if lot['completed_grid_cycle']}
    matches = []
    for m in fifo['matches']:
        buy = pd.Timestamp(by_id.loc[m['buy_event_id'], 'event_time'])
        sell = pd.Timestamp(by_id.loc[m['sell_event_id'], 'event_time'])
        category = 'risk_exit_net_at_sale' if m['purpose']=='RISK_EXIT' else 'closed_net_at_sale'
        if category=='risk_exit_net_at_sale' or m['buy_event_id'] in completed:
            out.loc[out.time == sell.floor('h'), category] += float(m['net'])
        matches.append(dict(buy=buy, sell=sell, completed=m['buy_event_id'] in completed,
            purpose=m['purpose'], net=float(m['net']),
            buy_utc_weekend=buy.dayofweek>=5, sell_utc_weekend=sell.dayofweek>=5,
            spans_weekend=bool((pd.date_range(buy.normalize(),sell.normalize(),freq='D').dayofweek>=5).any())))
    return out, pd.DataFrame(matches), fifo['summary']


def order_cohorts(source):
    """Final dispositions by first observed submission event, not by fill hour."""
    with sqlite3.connect((source/'paper.sqlite').resolve().as_uri()+'?mode=ro', uri=True) as db:
        state = decode(json.loads(db.execute('SELECT payload FROM snapshot').fetchone()[0]))
        events = [decode(json.loads(row[0])) for row in db.execute('SELECT payload FROM events')]
    first = {}
    for e in sorted(events, key=lambda e:e.event_time):
        first.setdefault(e.client_order_id, e.event_time)
    rows = []
    for order_id, record in state.orders.items():
        snapshot = record.snapshot
        rows.append(dict(time=pd.Timestamp(first[order_id]).floor('h'),
            any_fill=float(snapshot.cumulative_filled_quantity)>0,
            fully_filled=snapshot.status.value=='FILLED', canceled=snapshot.status.value=='CANCELED'))
    if not rows:
        return []
    frame=pd.DataFrame(rows).merge(labels([r['time'] for r in rows]).drop_duplicates(),on='time',validate='many_to_one')
    results=[]
    for field in ('utc_day','us_session'):
        for group,g in frame.groupby(field):
            results.append(dict(partition=field,group=group,submitted_orders=len(g),
                any_fill_orders=int(g.any_fill.sum()),filled_orders=int(g.fully_filled.sum()),
                canceled_orders=int(g.canceled.sum()),any_fill_rate=g.any_fill.mean(),
                full_fill_rate=g.fully_filled.mean(),cancellation_rate=g.canceled.mean()))
    return results


def uncertainty(values):
    values = np.asarray(values, dtype=float)
    rng = np.random.default_rng(707)
    means = rng.choice(values, size=(10000,len(values)), replace=True).mean(axis=1)
    starts = rng.integers(0,len(values),size=(10000,(len(values)+1)//2))
    blocks = np.stack((values[starts],values[(starts+1)%len(values)]),axis=-1).reshape(10000,-1)[:,:len(values)]
    block_means = blocks.mean(axis=1)
    return dict(n=len(values), mean=float(values.mean()),
        bootstrap_mean_q025=float(np.quantile(means,.025)),
        bootstrap_mean_q975=float(np.quantile(means,.975)),
        two_week_block_mean_q025=float(np.quantile(block_means,.025)),
        two_week_block_mean_q975=float(np.quantile(block_means,.975)),
        minimum=float(values.min()), maximum=float(values.max()),
        positive=int(np.sum(values>0)), zero=int(np.sum(values==0)),
        sum_without_best=float(values.sum()-values.max()),
        sum_without_worst=float(values.sum()-values.min()))


def run(args):
    output = Path(args.output)
    all_bars, identities = [], []
    for path in args.snapshots:
        bars, identity = load_hourly(path)
        manifest = json.loads((Path(path)/'manifest.json').read_text(encoding='utf-8'))
        identity['source'] = manifest.get('source', identity['source'])
        all_bars.extend(bars)
        identities.append(dict(path=path, **identity))
    summary, daily, weekends = market(all_bars)
    output.mkdir(parents=True, exist_ok=False)
    summary.to_csv(output/'market.csv', index=False)
    daily.to_csv(output/'market-daily.csv', index=False)
    weekends.to_csv(output/'market-weekends.csv', index=False)
    accounts, periods, matches, sensitivities, orders = [], [], [], [], []
    for window, root, fund in [('R04B',Path(args.old),Path(args.old_funding)),
                              ('R02',Path(args.new)/'accounts',Path(args.new)/'funding')]:
        for cost in ('base','stress'):
            for arm, mode in [('B','geometric'),('D','geometric'),('D','atr')]:
                name=f'{arm}/{mode}'
                source=root/cost/name
                hourly, matched, totals = account(source, fund/cost/name)
                orders.extend(dict(window=window,cost=cost,account=name,**r) for r in order_cohorts(source))
                for field in ('utc_day','us_session'):
                    for group, g in hourly.groupby(field):
                        accounts.append(dict(window=window,cost=cost,account=name,partition=field,group=group,
                            hours=len(g),pnl=g.pnl.sum(),price_carry=g.price_carry.sum(),fee=g.fee.sum(),
                            slippage=g.slippage.sum(),execution_reference=g.execution_reference.sum(),
                            funding=g.funding.sum(),adjusted_pnl=g.adjusted_pnl.sum(),
                            unrealized_change=g.unrealized_change.sum(),
                            closed_net_at_sale=g.closed_net_at_sale.sum(),risk_exit_net_at_sale=g.risk_exit_net_at_sale.sum(),
                            mean_inventory=g.inventory.mean(),max_inventory=g.inventory.abs().max(),
                            mean_notional=g.notional.mean(),max_notional=g.notional.max(),
                            paused_hours=g.paused.sum(),exited_hours=g.exited.sum()))
                for scale in (0,1,2,5):
                    adjusted=1000+(hourly.pnl+scale*hourly.funding).cumsum()
                    peaks=np.maximum.accumulate(np.r_[1000,adjusted])
                    dd=np.max(1-np.r_[1000,adjusted]/peaks)
                    sensitivities.append(dict(window=window,cost=cost,account=name,funding_multiplier=scale,
                        net_pnl=float(adjusted.iloc[-1]-1000),max_drawdown=dd,
                        closed_cycle_net=totals['completed_cycle']['net'],residual_net=totals['residual']['net'],
                        risk_exit_net=totals['risk_exit']['net']))
                w=hourly[hourly.utc_day!='weekday']
                for start,g in w.groupby(weekend_key(w.time)):
                    periods.append(dict(window=window,cost=cost,account=name,start=start,hours=len(g),
                        pnl=g.pnl.sum(),funding=g.funding.sum(),adjusted_pnl=g.adjusted_pnl.sum(),
                        closed_net_at_sale=g.closed_net_at_sale.sum(),price_carry=g.price_carry.sum(),
                        max_inventory=g.inventory.abs().max(),mean_notional=g.notional.mean(),
                        adjusted_return=g.adjusted_pnl.sum()/g.equity_before.iloc[0],
                        max_drawdown=float(np.max(1-np.r_[g.equity_before.iloc[0],g.adjusted_equity]/
                            np.maximum.accumulate(np.r_[g.equity_before.iloc[0],g.adjusted_equity])))))
                if len(matched):
                    matches.extend(matched.assign(window=window,cost=cost,account=name).to_dict('records'))
    pd.DataFrame(orders).to_csv(output/'order-cohorts.csv',index=False)
    pd.DataFrame(accounts).to_csv(output/'attribution.csv',index=False)
    pd.DataFrame(matches).to_csv(output/'fifo-matches.csv',index=False)
    pd.DataFrame(sensitivities).to_csv(output/'funding-sensitivity.csv',index=False)
    periods=pd.DataFrame(periods)
    periods.to_csv(output/'account-weekends.csv',index=False)
    stats=[]
    for (cost,name), g in periods.groupby(['cost','account']):
        # Strategies restart at the window boundary: combine only reporting blocks, not accounts.
        combined=g.groupby('start').agg(hours=('hours','sum'),adjusted_pnl=('adjusted_pnl','sum'))
        complete=combined[combined.hours==48]
        selected=g[g.hours==48]
        stats.append(dict(cost=cost,account=name,**uncertainty(complete.adjusted_pnl),
            arithmetic_mean_return=selected.adjusted_return.mean(),
            weekend_only_growth_factor=float(np.prod(1+selected.adjusted_return)),
            max_weekend_drawdown=selected.max_drawdown.max()))
    pd.DataFrame(stats).to_csv(output/'weekend-uncertainty.csv',index=False)
    result=dict(study='P7-R07-WEEKEND',boundaries=['NO LIVE MONEY','EXPLORATORY_RESEARCH'],
        inputs=identities,old=args.old,new=args.new,old_funding=args.old_funding,
        binary_models=[binary_model(c) for c in (0,.001,.002)],
        market_weekend_uncertainty=uncertainty(weekends.loc[weekends.complete,'close_return']),
        definitions=dict(extremes='absolute hourly log return >=1% or >=3%; not news events',
            breakout='close outside previous 24 hourly high/low envelope; first 24 hours excluded',
            uncertainty='10000 fixed-seed IID weekend-block percentile bootstrap; serial independence unproven',
            economic_sample='continuous frozen strategies, NOT fresh weekend-entry/exit experiments',
            calendar='2026 NYSE full holidays May25 Jun19 Jul03 Sep07; no early closes in window',
            fifo='completed-cycle net booked at sell time; not economic accrual within that hour',
            liquidity='hourly candles do not certify executable weekend order books',
            funding='saved historical posthoc overlay; no cash-constraint feedback',
            orders='read-only journal first-event submission cohorts and final snapshot dispositions; right-censored at window end'))
    (output/'result.json').write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')
    print(pd.DataFrame(stats).to_string(index=False))
    print(summary.to_string(index=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',nargs=2,required=True)
    parser.add_argument('--old',required=True)
    parser.add_argument('--old-funding',required=True)
    parser.add_argument('--new',required=True)
    parser.add_argument('--output',required=True)
    run(parser.parse_args())
