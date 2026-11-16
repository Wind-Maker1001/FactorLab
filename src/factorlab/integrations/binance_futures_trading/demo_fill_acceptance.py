"""Export actual Demo confirmations and attribute existing authoritative journal fills."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

from factorlab.errors import DataQualityError
from factorlab.experiments.grid_fifo import attribute_events
from factorlab.trading.storage import dumps, decode

from .demo_cli import open_session, recover
from .demo_credentials import DemoCredentialStore
from .demo_runtime import DemoRuntime, operator_lock, state_root

ZERO = Decimal(0)


def receipt_mapping(adapter):
    """Return persisted order/query/trade associations, never inferred trades."""
    latest = {}
    for client_id, observed_at, payload in adapter.db.execute(
            'SELECT client_id,observed_at,payload FROM futures_receipts ORDER BY rowid'):
        latest[client_id] = dict(observed_at=observed_at, **json.loads(payload))
    mapping = []
    for client_id, receipt in latest.items():
        order, trades = receipt['order'], receipt['trades']
        quantity = sum((Decimal(t['qty']) for t in trades), ZERO)
        quote = sum((Decimal(t['quoteQty']) for t in trades), ZERO)
        fee = sum((Decimal(t['commission']) for t in trades), ZERO)
        snapshot = adapter.order(client_id)
        if (order['clientOrderId'] != client_id or str(order['orderId']) != snapshot.exchange_order_id
                or any(str(t['orderId']) != str(order['orderId']) or t['symbol'] != order['symbol']
                       or t['commissionAsset'] != 'USDT' for t in trades)
                or len({t['id'] for t in trades}) != len(trades)
                or quantity != Decimal(order['executedQty']) or quote != Decimal(order['cumQuote'])
                or quantity != snapshot.cumulative_filled_quantity or fee != snapshot.cumulative_fee):
            raise DataQualityError('persisted actual receipt differs from confirmed order')
        mapping.append(dict(client_order_id=client_id, order_id=str(order['orderId']),
            intent=json.loads(dumps(snapshot.intent)), observed_at=receipt['observed_at'],
            receipt_source=receipt.get('source', 'UNCLASSIFIED_LEGACY'), order_query=order,
            trades=trades, trade_ids=[t['id'] for t in trades], trade_quantity_sum=quantity,
            trade_quote_sum=quote, actual_fee=fee, fee_asset='USDT' if trades else None))
    return mapping


def fifo_evidence(session, opening_wallet):
    """Adapt journal exports to the existing FIFO attribution engine."""
    state = session.gateway.state
    events = [event for _, event in session.controller.store.order_events() if event.fill_price is not None]
    previous, rows, closes = {}, [], {}
    # Export keys distinguish fills sharing an exchange timestamp; event_time remains original.
    epoch = datetime(2000, 1, 1)
    for index, event in enumerate(events):
        quantity_before, fee_before = previous.get(event.client_order_id, (ZERO, ZERO))
        quantity = event.cumulative_filled_quantity - quantity_before
        fee = event.cumulative_fee - fee_before
        previous[event.client_order_id] = event.cumulative_filled_quantity, event.cumulative_fee
        key = epoch + timedelta(microseconds=index)
        closes[key] = event.fill_price
        rows.append(dict(event_id=event.event_id, order_id=event.client_order_id,
            side=state.orders[event.client_order_id].snapshot.intent.side.value,
            event_time=event.event_time.isoformat(), execution_date=key.isoformat(),
            cumulative_filled_quantity=event.cumulative_filled_quantity, cumulative_fee=event.cumulative_fee,
            executed_shares=quantity, execution_price=event.fill_price, fee=fee,
            trade_notional=quantity * event.fill_price, slippage=ZERO, transaction_cost=fee))
    position = state.positions.get('NVDAUSDT')
    mark = position.mark_price if position else ZERO
    equity = sum(state.cash.values(), ZERO) + sum(
        (p.quantity * (p.mark_price - p.entry_price) for p in state.positions.values()), ZERO)
    result = dict(config=dict(initial_inventory=0, initial_price=0), rules=dict(quantity_unit=1),
        orders={key: {} for key in state.orders}, instrument_id='NVDAUSDT', market='BINANCE_USDM',
        capital=opening_wallet, final=dict(close=mark, equity=equity))
    attributed = attribute_events(result, rows, closes, events, state)
    return dict(source='AUTHORITATIVE_CONTROLLER_EVENTS', attribution=attributed,
        slippage='NOT_OBSERVED; execution prices are attribution references',
        economic_events=[dict(event_id=e.event_id, client_order_id=e.client_order_id,
            cumulative_quantity=e.cumulative_filled_quantity, cumulative_fee=e.cumulative_fee,
            fill_price=e.fill_price, event_time=e.event_time) for e in events])


def capture(session, runtime, opening_wallet):
    account = recover(session)
    mapping = receipt_mapping(session.adapter)
    fifo = fifo_evidence(session, opening_wallet)
    income_fields = {'symbol', 'incomeType', 'income', 'asset', 'time', 'tranId', 'tradeId'}
    income = [{key: value for key, value in row.items() if key in income_fields}
              for row in session.adapter.income('NVDAUSDT')]
    funding = [row for row in income if row['incomeType'] == 'FUNDING_FEE']
    summary = fifo['attribution']['summary']
    filled = [row for row in mapping if row['trade_quantity_sum'] > 0]
    inventory = sum((p['quantity'] for p in account['positions']), ZERO)
    fees = sum((row['actual_fee'] for row in mapping), ZERO)
    journal_fees = sum((r.snapshot.cumulative_fee for r in session.gateway.state.orders.values()), ZERO)
    fee_ok = bool(filled) and fees == journal_fees == summary['actual_fees']
    position_ok = bool(filled) and inventory == summary['residual_inventory']
    wallet = account['balances']['walletBalance']
    equity = wallet + account['balances']['unrealizedProfit']
    # TODO: attribute confirmed funding settlements through the authoritative ledger before accepting funded accounts.
    account_ok = (fee_ok and position_ok and account['reconciled']
                  and summary['reconciliation_residual'] == 0
                  and summary['final_equity'] == equity and not funding)
    dispatches = [dict(campaign=c, method=m, client_order_id=i, parameters=json.loads(p),
                       receipt=json.loads(r) if r is not None else None)
                  for c, m, i, p, r in runtime.db.execute(
                      'SELECT campaign,method,client_id,parameters,receipt FROM dispatches ORDER BY rowid')]
    history = [json.loads(row[0]) for row in session.adapter.db.execute(
        'SELECT payload FROM futures_receipts ORDER BY rowid')]
    queries = [receipt['order'] for receipt in history if receipt.get('source') == 'GET']
    observed = {row['status'] for row in queries}
    observed.update(d['receipt']['status'] for d in dispatches
                    if d['receipt'] is not None and d['receipt'].get('status') is not None)
    unknown = [d['client_order_id'] for d in dispatches if d['method'] == 'POST'
               and (d['receipt'] is None or d['receipt']['http_status'] is None)]
    queried = {row['clientOrderId'] for row in queries}
    completed_get = any(row['status'] == 'FILLED' for row in queries)
    race = any(d['method'] == 'DELETE' and d['receipt'] is not None
               and d['receipt'].get('status') == 'FILLED' for d in dispatches)
    race |= any(d['method'] == 'DELETE' and d['receipt'] is not None
                and d['receipt'].get('status') == 'CANCELED'
                and any(row['clientOrderId'] == d['client_order_id'] and row['status'] == 'FILLED'
                        for row in queries) for d in dispatches)
    lifecycle = dict(status_observations=sorted(observed),
        unknown_ack_queried=bool(unknown) and set(unknown) <= queried,
        completed_order_get=completed_get, cancel_fill_race_observed=race)
    lifecycle_ok = ({'NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED'} <= observed
                    and lifecycle['unknown_ack_queried'] and completed_get and race)
    statuses = dict(DEMO_ORDER_ACK_VERIFIED=any(
        d['method'] == 'POST' and d['receipt'] is not None and d['receipt']['http_status'] == 200
        and d['receipt'].get('order_id') is not None for d in dispatches),
        DEMO_TRADE_RECEIPT_VERIFIED=bool(filled), DEMO_FEE_RECONCILED=fee_ok,
        DEMO_POSITION_RECONCILED=position_ok, DEMO_ACCOUNT_RECONCILED=account_ok,
        DEMO_RESTART_RECOVERY_VERIFIED=False, DEMO_LIFECYCLE_VERIFIED=lifecycle_ok)
    return dict(observed_at=datetime.now(timezone.utc), source='BINANCE_DEMO_ACTUAL_GET_RECEIPTS',
        scope='ALL_PERSISTED_ACCOUNT_HISTORY; not proof of a new experiment',
        opening_wallet=opening_wallet, statuses=statuses, account=account,
        order_trade_mapping=mapping, dispatches=dispatches, fifo=fifo,
        wallet=wallet, equity=equity, actual_fees=fees, income=income,
        funding=dict(events=funding, booking='NOT_SUPPORTED' if funding else 'NO_EVENT_OBSERVED_IN_RESPONSE'),
        observed_order_statuses=sorted({row['order_query']['status'] for row in mapping}),
        lifecycle=lifecycle,
        recovery_proof=dict(account_id=session.adapter.account_id,
            protocol_events=session.adapter.db.execute(
            'SELECT count(*) FROM futures_events').fetchone()[0],
            economic_events=fifo['economic_events'], state=dumps(session.gateway.state),
            dispatch_count=len(dispatches)),
        safety='NO LIVE MONEY')


def compare_recovery(previous, current):
    """Compare separately captured recoveries without interpreting marks as fills."""
    report = current
    previous = json.loads(json.dumps(previous, default=str))
    current = json.loads(json.dumps(current, default=str))
    old_state = decode(json.loads(previous['recovery_proof']['state']))
    new_state = decode(json.loads(current['recovery_proof']['state']))
    same_identity = old_state.account_id == new_state.account_id
    old_orders = {key: record.snapshot.intent for key, record in old_state.orders.items()}
    new_orders = {key: record.snapshot.intent for key, record in new_state.orders.items()}
    facts = dict(same_account=same_identity, historical_intents_preserved=old_orders == new_orders,
        economic_events_unchanged=previous['fifo']['economic_events'] == current['fifo']['economic_events'],
        no_new_dispatch=previous['recovery_proof']['dispatch_count'] == current['recovery_proof']['dispatch_count'],
        wallet_unchanged=previous['wallet'] == current['wallet'],
        fees_unchanged=previous['actual_fees'] == current['actual_fees'],
        reconciled=current['account']['reconciled'] and not current['account']['unknown'],
        get_only=current['account']['requests']['POST'] == current['account']['requests']['DELETE'] == 0)
    report['restart_comparison'] = facts
    report['statuses']['DEMO_RESTART_RECOVERY_VERIFIED'] = all(facts.values())
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opening-wallet', required=True, type=Decimal,
                        help='documented initially flat wallet baseline; never inferred from current balance')
    parser.add_argument('--output', required=True, type=Path, help='new private evidence file')
    parser.add_argument('--previous', type=Path, help='earlier private capture from a separate session/process')
    args = parser.parse_args(argv)
    previous = json.loads(args.previous.read_text(encoding='utf-8')) if args.previous else None
    if not args.opening_wallet.is_finite() or args.opening_wallet <= 0:
        parser.error('opening wallet must be positive and finite')
    # Reserve the evidence file before any network operation.
    with args.output.open('x', encoding='utf-8') as output:
        root = state_root()
        with operator_lock(root), closing(DemoRuntime(root)) as runtime:
            with open_session(root, DemoCredentialStore(), runtime, dict(GET=0, POST=0, DELETE=0)) as (session, _):
                report = capture(session, runtime, args.opening_wallet)
        if previous is not None:
            compare_recovery(previous, report)
        json.dump(report, output, indent=2, default=str)
        output.write('\n')
    print(json.dumps(report['statuses']))


if __name__ == '__main__':
    main()
