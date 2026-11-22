"""Research-only FIFO attribution of authoritative long-only Paper fills."""
from collections import deque
import csv
from decimal import Decimal
from datetime import datetime
import json
from pathlib import Path

from factorlab.trading.storage import PaperStore


ZERO = Decimal(0)


def D(value):
    return Decimal(str(value))


def _economics(quantity, buy_price, sell_price, buy_fee, sell_fee, buy_slip, sell_slip):
    fees = buy_fee + sell_fee
    slip = buy_slip + sell_slip
    executed = quantity * (sell_price - buy_price)
    return dict(quantity=quantity, reference_gross=executed + slip,
                execution_gross=executed, buy_fee=buy_fee, sell_fee=sell_fee,
                buy_slippage=buy_slip, sell_slippage=sell_slip,
                fees=fees, slippage=slip, net=executed-fees)


def attribution(directory):
    """Return Decimal-valued ``summary``, FIFO ``matches`` and BUY-fill ``lots``.

    A lot is one authoritative BUY fill, not an order or a matched fragment.
    Only fully consumed lots untouched by RISK_EXIT count as grid_cycles.
    Normal-sell portions of incomplete/contaminated lots remain in normal_sell
    economics, but never enter completed_cycle economics. Slippage is embedded
    in fill prices: reference_gross - fees - slippage equals net.
    Funding is not booked or inferred. Endowed inventory is not a BUY cycle.
    """
    directory = Path(directory)
    result = json.loads((directory/'result.json').read_text(encoding='utf-8'))
    with (directory/'trades.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    with (directory/'daily.csv').open(newline='', encoding='utf-8') as stream:
        daily = list(csv.DictReader(stream))
    closes = {datetime.fromisoformat(row['date']): D(row['close']) for row in daily}
    store = PaperStore(directory/'paper.sqlite')
    try:
        events = [event for _, event in store.order_events() if event.fill_price is not None]
        funding = [event for _, event in store.funding_events()] if hasattr(store, 'funding_events') else []
        state = store.load()
    finally:
        store.close()
    return attribute_events(result, rows, closes, events, state, funding_events=funding or None)


def attribute_events(result, rows, closes, events, state, *, funding_events=None):
    """Attribute exported authoritative fills without opening a research account.

    ``closes`` supplies each fill's explicit reference price, keyed by its export
    timestamp. Using execution prices as references attributes execution P/L only;
    it does not establish market slippage. Confirmed ``funding_events``, when
    supplied, enter account/strategy net P/L independently without altering fill
    quantities, fill prices, FIFO lot cost basis or order fees.
    """
    if len(rows) != len(events) or len({r['event_id'] for r in rows}) != len(rows):
        raise ValueError('fill log differs from authoritative Paper events')
    by_id = {row['event_id']: row for row in rows}
    lots, matches, queue = [], [], deque()
    initial = D(result['config']['initial_inventory']) * D(result['rules']['quantity_unit'])
    if initial:
        lot = dict(event_id=None, order_id=None, quantity=initial, remaining=initial,
                   price=D(result['config']['initial_price']), fee=ZERO, slippage=ZERO,
                   risk_contaminated=False, endowed=True)
        lots.append(lot)
        queue.append(lot)
    previous = {}
    total_fees = total_slippage = ZERO
    for event in events:
        row = by_id[event.event_id]
        order_id = event.client_order_id
        order = result['orders'][order_id]
        side = state.orders[order_id].snapshot.intent.side.value
        old_quantity, old_fee = previous.get(order_id, (ZERO, ZERO))
        quantity = event.cumulative_filled_quantity - old_quantity
        fee = event.cumulative_fee - old_fee
        previous[order_id] = event.cumulative_filled_quantity, event.cumulative_fee
        price = event.fill_price
        close = closes[datetime.fromisoformat(row['execution_date'])]
        slip = quantity * abs(price-close)
        # CSV numerics were exported through float; compare at that boundary.
        expected = dict(executed_shares=quantity, execution_price=price, fee=fee,
                        trade_notional=quantity*price, slippage=slip,
                        transaction_cost=fee+slip)
        if (row['order_id'] != order_id or row['side'] != side
                or row['event_time'] != event.event_time.isoformat()
                or D(row['cumulative_filled_quantity']) != event.cumulative_filled_quantity
                or D(row['cumulative_fee']) != event.cumulative_fee
                or any(float(row[key]) != float(value) for key, value in expected.items())):
            raise ValueError('fill row differs from authoritative Paper event or close')
        total_fees += fee
        total_slippage += slip
        if side == 'BUY':
            lot = dict(event_id=event.event_id, order_id=order_id, quantity=quantity,
                       remaining=quantity, price=price, fee=fee, slippage=slip,
                       risk_contaminated=False, endowed=False)
            lots.append(lot)
            queue.append(lot)
            continue
        risk = order.get('purpose') == 'RISK_EXIT'
        remaining, fee_left, slip_left = quantity, fee, slip
        while remaining:
            lot = queue[0]
            matched = min(remaining, lot['remaining'])
            buy_fee = lot['fee'] if matched == lot['remaining'] else lot['fee']*matched/lot['remaining']
            buy_slip = lot['slippage'] if matched == lot['remaining'] else lot['slippage']*matched/lot['remaining']
            sell_fee = fee_left if matched == remaining else fee_left*matched/remaining
            sell_slip = slip_left if matched == remaining else slip_left*matched/remaining
            matches.append(dict(buy_event_id=lot['event_id'], sell_event_id=event.event_id,
                                sell_order_id=order_id, purpose='RISK_EXIT' if risk else 'GRID',
                                endowed=lot['endowed'], **_economics(matched, lot['price'], price,
                                    buy_fee, sell_fee, buy_slip, sell_slip)))
            lot['risk_contaminated'] |= risk
            lot['remaining'] -= matched
            lot['fee'] -= buy_fee
            lot['slippage'] -= buy_slip
            remaining -= matched
            fee_left -= sell_fee
            slip_left -= sell_slip
            if not lot['remaining']:
                queue.popleft()
    completed = {lot['event_id'] for lot in lots if not lot['remaining']
                 and not lot['risk_contaminated'] and not lot['endowed']}
    mark = D(result['final']['close'])
    residual = [_economics(lot['remaining'], lot['price'], mark,
                          lot['fee'], ZERO, lot['slippage'], ZERO) for lot in queue]
    keys = ('quantity', 'reference_gross', 'execution_gross', 'buy_fee', 'sell_fee',
            'buy_slippage', 'sell_slippage', 'fees', 'slippage', 'net')
    def total(items):
        items = list(items)
        return {key: sum((item[key] for item in items), ZERO) for key in keys}
    normal = total(m for m in matches if m['purpose'] != 'RISK_EXIT')
    exits = total(m for m in matches if m['purpose'] == 'RISK_EXIT')
    residual_total = total(residual)
    if funding_events is not None:
        from factorlab.trading.contracts import FundingEvent
        funding_list = list(funding_events)
        if any(not isinstance(event, FundingEvent) for event in funding_list):
            raise ValueError('funding_events must contain FundingEvent instances')
        if len({event.event_id for event in funding_list}) != len(funding_list):
            raise ValueError('duplicate funding event_id in attribution')
        realized_funding = sum((event.amount for event in funding_list), ZERO)
        funding_by_symbol, funding_by_asset = {}, {}
        for event in funding_list:
            funding_by_symbol[event.symbol] = funding_by_symbol.get(event.symbol, ZERO) + event.amount
            funding_by_asset[event.asset] = funding_by_asset.get(event.asset, ZERO) + event.amount
        funding_summary = dict(
            source='AUTHORITATIVE_FUNDING_LEDGER',
            event_count=len(funding_list),
            realized_funding=realized_funding,
            by_symbol=funding_by_symbol,
            by_asset=funding_by_asset,
        )
    else:
        realized_funding = ZERO
        funding_summary = 'NOT_SIMULATED'
    realized_trade_pnl = normal['execution_gross'] + exits['execution_gross']
    realized_trade_net = normal['net'] + exits['net']
    trading_net = normal['net'] + exits['net'] + residual_total['net']
    strategy_net_pnl = trading_net + realized_funding
    economic_net = strategy_net_pnl
    quantity = sum((lot['remaining'] for lot in queue), ZERO)
    position = state.positions.get(result['instrument_id'])
    valuation_adjustment = quantity * (position.mark_price-mark) if position else ZERO
    capital = D(result['capital'])
    final_wallet = sum(state.cash.values(), ZERO)
    wallet_change = final_wallet - capital
    final_equity = final_wallet
    if result['market'] == 'BINANCE_EQUITY':
        final_equity += sum((p.quantity*p.mark_price for p in state.positions.values()), ZERO)
    else:
        final_equity += sum((p.quantity*(p.mark_price-p.entry_price) for p in state.positions.values()), ZERO)
    summary = dict(grid_cycles=len(completed), buy_fill_lots=sum(not l['endowed'] for l in lots),
                   normal_sell=normal, risk_exit=exits, residual=residual_total,
                   completed_cycle=total(m for m in matches if m['buy_event_id'] in completed),
                   residual_inventory=quantity, actual_fees=total_fees,
                   actual_slippage=total_slippage,
                   realized_trade_pnl=realized_trade_pnl,
                   realized_trade_net=realized_trade_net,
                   realized_funding=realized_funding,
                   trading_net=trading_net,
                   strategy_net_pnl=strategy_net_pnl,
                   economic_net=economic_net,
                   valuation_adjustment=valuation_adjustment,
                   final_wallet=final_wallet,
                   wallet_change=wallet_change,
                   final_equity=final_equity,
                   final_nav=final_equity/capital,
                   reconciliation_residual=final_equity-capital-economic_net-valuation_adjustment,
                   reported_equity_difference=D(result['final']['equity'])-final_equity,
                   funding=funding_summary)
    for lot in lots:
        lot['completed_grid_cycle'] = lot['event_id'] in completed and not lot['endowed']
    return dict(summary=summary, matches=matches, lots=lots)
