"""Offline R06 passive controls; never executes the original grid accounts."""
import argparse
from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from factorlab.experiments.grid_paper import D, _write
from factorlab.experiments.grid_independent import load_sample
from factorlab.trading.contracts import OrderType, Side
from factorlab.trading.execution import execution_candidate

STEP = D('.01')
CAPITAL = D(1000)


def floor_step(value):
    return value // STEP * STEP


def chunks(quantity, limit):
    """Partition without leaving an illegal sub-minimum final order."""
    minimum = max(STEP, (D(5) / limit / STEP).to_integral_value(rounding='ROUND_CEILING') * STEP)
    if not quantity:
        return []
    count = int((quantity / D('.1')).to_integral_value(rounding='ROUND_CEILING'))
    if quantity < count * minimum:
        raise ValueError('target cannot be partitioned into legal minimum-notional orders')
    result = []
    for remaining_orders in range(count - 1, -1, -1):
        part = min(D('.1'), quantity - remaining_orders * minimum)
        result.append(part)
        quantity -= part
    return result


def simulate(bars, *, fee_bps=4, slip_bps=6, quantity=None, targets=None):
    """Persistent initial LIMITs or one-hour cancel/replace lagged target LIMITs.

    Dynamic targets are grid-dependent, not independent buy-and-hold signals.
    No order generated at a close can trade at that same close. USDM equity is
    represented by a fully collateralized cash-equivalent ledger, with committed
    capital measured as entry basis plus outstanding buy reservations.
    """
    fee, slip = D(fee_bps)/10000, D(slip_bps)/10000
    cash, position, basis = CAPITAL, D(0), D(0)
    orders, rows, fills = [], [], []
    for i, bar in enumerate(bars):
        close = D(bar['close'])
        budget = D(int(D(bar['volume']))) * D('.01')
        active = []
        for order in orders:
            candidate = execution_candidate(order, ask=close, bid=close, budget=budget,
                                            slippage=slip, step=STEP)
            if candidate:
                q, price = candidate
                if order.intent.side == Side.BUY:
                    # Full collateral budget, including already filled entry basis.
                    q = min(q, floor_step(max(D(0), CAPITAL-basis)/(price*(1+fee))),
                            floor_step(max(D(0), cash)/(price*(1+fee))))
                if q:
                    cost = q*price*fee
                    if order.intent.side == Side.BUY:
                        cash -= q*price + cost
                        basis += q*price
                        position += q
                    else:
                        basis -= basis*q/position
                        position -= q
                        cash += q*price-cost
                    budget -= q
                    order.cumulative_filled_quantity += q
                    fills.append(dict(execution_date=bar['time'], side=order.intent.side.value,
                        executed_shares=float(q), execution_price=float(price), fee=float(cost),
                        slippage=float(q*abs(price-close)), trade_notional=float(q*price),
                        transaction_cost=float(cost+q*abs(price-close))))
            if order.cumulative_filled_quantity < order.intent.quantity:
                active.append(order)
        orders = active if quantity is not None else []
        if i == 0 and quantity is not None or targets is not None:
            target = D(quantity) if quantity is not None else D(targets[i])
            target = floor_step(min(D(1), target, CAPITAL/(close*(1+slip)*(1+fee))))
            delta = target-position
            side = Side.BUY if delta >= 0 else Side.SELL
            limit = (close*(1+slip) // STEP * STEP if side == Side.BUY else
                     (close*(1-slip)/STEP).to_integral_value(rounding='ROUND_CEILING')*STEP)
            try:
                parts = chunks(abs(delta), limit)
            except ValueError:
                if quantity is not None:
                    raise
                # A lagged target may require an untradeable dust adjustment.
                parts = []
            for q in parts:
                orders.append(SimpleNamespace(intent=SimpleNamespace(side=side,
                    order_type=OrderType.LIMIT, limit_price=limit, quantity=q),
                    cumulative_filled_quantity=D(0)))
        reserved = sum(((o.intent.quantity-o.cumulative_filled_quantity)*o.intent.limit_price*(1+fee)
                        for o in orders if o.intent.side == Side.BUY), D(0))
        rows.append(dict(date=bar['time'], close=float(close), inventory=float(position),
            equity=float(cash+position*close), net_exposure=float(position*close),
            committed=float(basis+reserved), cash=float(cash)))
    columns = ['execution_date', 'side', 'executed_shares', 'execution_price', 'fee',
               'slippage', 'trade_notional', 'transaction_cost']
    return pd.DataFrame(rows), pd.DataFrame(fills, columns=columns)


def fifo_control(fills, mark):
    lots, matched = deque(), D(0)
    for row in fills.itertuples():
        q, price, fee = map(D, (row.executed_shares, row.execution_price, row.fee))
        if row.side == 'BUY':
            lots.append([q, price, fee])
        else:
            original = q
            while q:
                lot = lots[0]
                take = min(q, lot[0])
                buy_fee = lot[2]*take/lot[0]
                matched += take*(price-lot[1])-buy_fee-fee*take/original
                lot[0] -= take
                lot[2] -= buy_fee
                q -= take
                if not lot[0]:
                    lots.popleft()
    residual = sum((q*(D(mark)-price)-fee for q, price, fee in lots), D(0))
    return dict(sold_fifo_net=float(matched), residual_net=float(residual))


def stats(nav, fills):
    equity = np.r_[1000., nav.equity.to_numpy()]
    returns = np.diff(equity)/equity[:-1]
    vol = float(np.std(returns, ddof=1))
    down = float(np.sqrt(np.mean(np.minimum(returns, 0)**2)))
    pnl = float(equity[-1]-1000)
    return dict(net_pnl=pnl, net_return=pnl/1000,
        max_drawdown=float(np.max(1-equity/np.maximum.accumulate(equity))),
        mean_notional=float(nav.net_exposure.mean()), max_notional=float(nav.net_exposure.max()),
        max_committed=float(nav.committed.max()), holding_fraction=float(nav.inventory.ne(0).mean()),
        fills=len(fills), turnover_usdt=float(fills.trade_notional.sum()),
        turnover_initial_capital=float(fills.trade_notional.sum()/1000),
        fees=float(fills.fee.sum()), slippage=float(fills.slippage.sum()),
        total_cost=float(fills.transaction_cost.sum()), hourly_volatility=vol,
        annualized_volatility=vol*np.sqrt(8760), downside_hourly=down,
        sharpe=float(np.mean(returns)/vol*np.sqrt(8760)) if vol else None,
        sortino=float(np.mean(returns)/down*np.sqrt(8760)) if down else None)


def run(evidence, snapshot, output):
    evidence, output = Path(evidence), Path(output)
    bars, identity = load_sample(snapshot)
    output.mkdir(parents=True, exist_ok=False)
    _write(output/'design.json', dict(study='P7-R06', boundaries=['NO LIVE MONEY',
        'EXPLORATORY_RESEARCH'], funding='EXCLUDED_BOTH_SIDES', initial_capital=1000,
        window=[identity['start'], identity['end_exclusive']], hours=len(bars),
        ladder_units=[i/100 for i in range(1,101)],
        rule='Frozen first-close limit; dynamic target t submitted after t fills, earliest t+1; cancel/replace dynamic limits each close',
        minimum_notional=5, quantity_step=.01, max_order=.1, max_inventory=1,
        participation=.01, annualization_hours=8760, risk_free_rate=0,
        return_period='hourly including initial NAV',
        design_status='RETROSPECTIVE_RULE_DESIGN_NOT_HISTORICAL_PREREGISTRATION'))
    rows, comparisons, posthoc = [], [], []
    for scenario, fee, slip in [('base',4,6), ('stress',8,12)]:
        ladder = []
        for units in range(1,101):
            name = f'fixed_{units:03d}'
            try:
                nav, fills = simulate(bars, fee_bps=fee, slip_bps=slip, quantity=D(units)/100)
            except ValueError as error:
                rows.append(dict(scenario=scenario, name=name, kind='EX_ANTE_FIXED',
                                 status='INFEASIBLE', reason=str(error)))
                continue
            record = dict(scenario=scenario, name=name, kind='EX_ANTE_FIXED', status='OK',
                          **stats(nav,fills), **fifo_control(fills,nav.close.iloc[-1]))
            rows.append(record)
            ladder.append(record)
            # Preserve the entire new hourly benchmark evidence outside Git.
            directory = output/scenario/name
            directory.mkdir(parents=True)
            nav.to_csv(directory/'hourly.csv',index=False)
            fills.to_csv(directory/'fills.csv',index=False)
        for arm, mode in [('cash','arithmetic'),('fixed_budget_buy_hold','arithmetic')]+[
                (a,m) for a in ('B','D') for m in ('arithmetic','geometric','atr')]:
            source = evidence/scenario/arm/mode
            nav = pd.read_csv(source/'daily.csv')
            fills = pd.read_csv(source/'trades.csv')
            economics = json.loads((source/'fifo.json').read_text())['summary']
            name = f'{arm}_{mode}'
            original = dict(scenario=scenario,name=name,kind='EXISTING_R04B',status='OK',
                **stats(nav,fills), completed_cycle_net=float(economics['completed_cycle']['net']),
                normal_sell_net=float(economics['normal_sell']['net']),
                risk_exit_net=float(economics['risk_exit']['net']),
                residual_net=float(economics['residual']['net']))
            rows.append(original)
            if arm not in ('B','D'):
                continue
            lag, lag_fills = simulate(bars, fee_bps=fee, slip_bps=slip,
                                     targets=nav.inventory.tolist())
            lag_record = dict(scenario=scenario,name='lag_'+name,
                kind='EX_ANTE_GRID_DEPENDENT_LAGGED',status='OK', **stats(lag,lag_fills),
                **fifo_control(lag_fills,lag.close.iloc[-1]))
            rows.append(lag_record)
            directory = output/scenario/('lag_'+name)
            directory.mkdir(parents=True)
            lag.to_csv(directory/'hourly.csv',index=False)
            lag_fills.to_csv(directory/'fills.csv',index=False)
            below = [r for r in ladder if r['mean_notional'] <= original['mean_notional']]
            above = [r for r in ladder if r['mean_notional'] >= original['mean_notional']]
            controls = ([max(below,key=lambda r:r['mean_notional'])] if below else [])+(
                [min(above,key=lambda r:r['mean_notional'])] if above else [])+[lag_record]
            for control in controls:
                comparisons.append(dict(scenario=scenario,grid=name,benchmark=control['name'],
                    benchmark_kind=control['kind'], grid_mean=original['mean_notional'],
                    benchmark_mean=control['mean_notional'], grid_pnl=original['net_pnl'],
                    benchmark_pnl=control['net_pnl'], advantage=original['net_pnl']-control['net_pnl'],
                    bracket_status='BRACKETED' if below and above else 'OUTSIDE_FEASIBLE_LADDER'))
            # Exact accounting identity, NOT an investable same-exposure portfolio.
            price = nav.close.to_numpy()
            q = nav.inventory.to_numpy()
            carry = float(np.sum(q[:-1]*np.diff(price)))
            execution = original['net_pnl']-carry+original['total_cost']
            posthoc.append(dict(scenario=scenario,grid=name,kind='POSTHOC_ATTRIBUTION',
                prior_inventory_price_pnl=carry, execution_timing_reference_gross=execution,
                fees=original['fees'], slippage=original['slippage'], net_pnl=original['net_pnl'],
                identity='net = prior_inventory_price_pnl + execution_timing_reference_gross - fees - slippage',
                interpretation='Not executable: reconstructs realized path using same-hour fills; execution component includes limit timing'))
    pd.DataFrame(rows).to_csv(output/'metrics.csv',index=False)
    pd.DataFrame(comparisons).to_csv(output/'comparisons.csv',index=False)
    pd.DataFrame(posthoc).to_csv(output/'posthoc.csv',index=False)
    _write(output/'result.json', conclusions(rows, comparisons))
    print(pd.DataFrame(comparisons).to_string(index=False))


def conclusions(rows, comparisons):
    geometric = [r for r in rows if r['name'] == 'B_geometric']
    contrasts = [r for r in comparisons if r['grid'] == 'B_geometric']
    fixed_positive = all(r['advantage'] > 0 for r in contrasts if r['benchmark_kind'] == 'EX_ANTE_FIXED')
    lag_positive = all(r['advantage'] > 0 for r in contrasts if r['benchmark_kind'] == 'EX_ANTE_GRID_DEPENDENT_LAGGED')
    return dict(status='INCONCLUSIVE',
        historical_evidence=('POSITIVE_ACROSS_CONTROLS' if fixed_positive and lag_positive
                             else 'BENCHMARK_METHOD_DEPENDENT'),
        above_cash_both_costs=all(r['net_pnl'] > 0 for r in geometric),
        above_adjacent_fixed_both_costs=fixed_positive,
        above_lagged_control_both_costs=lag_positive,
        completed_cycles_dominate_both_costs=all(r['completed_cycle_net'] > r['net_pnl'] > 0 for r in geometric),
        question='Positive economic advantage over comparable passive exposure',
        explanation='Historical control comparisons are method dependent unless all controls agree; single-window evidence remains exploratory even if positive.',
        boundaries=['NO LIVE MONEY','EXPLORATORY_RESEARCH'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--evidence',required=True)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    run(args.evidence,args.snapshot,args.output)
