"""Explicit bounded Grid submission and restart-safe read-only recovery."""
import argparse
from contextlib import closing
from datetime import timezone
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
import json

from factorlab.integrations.binance_futures_trading.demo_credentials import DemoCredentialStore
from factorlab.integrations.binance_futures_trading.demo_grid_bridge import minimum_quantity
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, SMOKE_CAMPAIGN, open_grid_session, state_root, restore_grid, saved_grid_deployment
from factorlab.trading.storage import decode
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.trading import Market


def factory(session, deployment_id):
    saved = saved_grid_deployment(session.controller.store)
    if saved is not None:
        deployment = decode(json.loads(saved))['deployment']
        if deployment['deployment_id'] == deployment_id:
            return restore_grid(deployment)
    session.recover()
    status = session.status()
    if not status['reconciled'] or status['positions'] or status['open_orders'] or status['unknown']:
        raise SafetyBlocked('Grid acceptance requires reconciled flat account without active or UNKNOWN orders')
    book = session._fresh_book()
    filters = session.gateway.rules.filters
    lot, price_filter = filters['LOT_SIZE'], filters['PRICE_FILTER']
    tick = D(price_filter['tickSize'])
    price = book['askPrice']
    lower = (price * D('.99') / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    upper = (price * D('1.01') / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    rules = GridRules(D('.04'), D(lot['stepSize']), tick, D(lot['minQty']), D(filters['MIN_NOTIONAL']['notional']))
    if max(minimum_quantity(rules, lower), minimum_quantity(rules, upper)) > D('.04') or D('.04') * max(upper, price) > 7:
        raise SafetyBlocked('Minimum legal Grid pair exceeds unchanged quantity or notional budget')
    print(json.dumps(dict(kind='GRID_CONFIGURATION', lower=str(lower), upper=str(upper), quantity='.04', book=book), default=str), flush=True)
    return GridExecution(GridConfig(lower=float(lower), upper=float(upper), levels=1,
        initial_price=float(price), initial_inventory=0, quantity=1, max_inventory=1,
        max_cash_committed=7, breakout='pause'), rules, deployment_id=deployment_id,
        account_id=session.adapter.account_id, market=Market.BINANCE_USDM,
        instrument_id='NVDAUSDT', initial_equity=D(status['balances']['walletBalance']))


def initial_observation(book):
    return dict(id='bounded-grid-real-book',
        time=book['time_utc'].astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        price=str(book['askPrice']), volume=0)


def submit_initial(bridge):
    key = bridge.prefix + 'initial-observation'
    raw = bridge.controller.store.event_payload(key)
    if raw is None:
        event = initial_observation(bridge.session._fresh_book())
        with bridge.controller.store.transaction():
            bridge.controller.store.record_metadata(key, event)
    else:
        event = decode(json.loads(raw))
    # Command replay already includes this observation, even after an unknown POST.
    for (payload,) in bridge.controller.store.db.execute(
            'SELECT payload FROM events WHERE id LIKE ?', (bridge.prefix + 'command:%',)):
        command = decode(json.loads(payload))
        if command['action'] == 'observe' and command['value']['id'] == event['id']:
            return None
    return bridge.observe(event)


def execute(bridge, command, client_order_id=None):
    bridge.recover()
    result = None
    if command == 'submit':
        result = submit_initial(bridge)
    elif command == 'cancel':
        if not bridge.grid._owns(client_order_id) or not any(
                order.intent.client_order_id == client_order_id
                for order in bridge.controller.account().open_orders):
            raise SafetyBlocked('cancel requires a known open order owned by this Grid deployment')
        result = bridge.session.cancel(client_order_id)
        bridge.recover()
    binding = decode(json.loads(saved_grid_deployment(bridge.controller.store)))
    return dict(kind='GRID_' + command.upper(), result=str(result), campaign=binding['campaign'],
        deployment=bridge.grid.deployment(), account=bridge.session.status())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='status', choices=('status', 'recover', 'submit', 'cancel'))
    parser.add_argument('client_order_id', nargs='?')
    parser.add_argument('--new-deployment', action='store_true',
        help='explicitly replace a previous campaign deployment, only while reconciled and flat')
    args = parser.parse_args(argv)
    if args.new_deployment and args.command != 'submit':
        parser.error('--new-deployment is only valid for submit')
    if (args.command == 'cancel') != (args.client_order_id is not None):
        parser.error('cancel requires CLIENT_ORDER_ID; other commands accept no order ID')
    root = state_root()
    with open_grid_session(root, DemoCredentialStore(), factory,
            read_only=args.command in ('status', 'recover'), new_deployment=args.new_deployment) as bridge:
        report = execute(bridge, args.command, args.client_order_id)
        with closing(DemoRuntime(root)) as runtime:
            campaign = runtime.campaign(report['campaign'])
            report['budget'] = dict(campaign=campaign['id'], expires_at=campaign['expires_at'],
                post_budget=campaign['orders'], posts_consumed=runtime.post_count(campaign['id']),
                active=runtime.active_campaign() == campaign)
            report['write_receipts'] = runtime.receipts(campaign['id'])
        print(json.dumps(report, default=str), flush=True)


if __name__ == '__main__':
    main()
