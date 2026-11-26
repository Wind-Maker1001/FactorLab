"""Per-instance native grid projections from the shared capital authority."""
from decimal import Decimal as D
import json

from factorlab.trading import Market
from factorlab.trading.storage import decode
from .protocol import SafetyBlocked


def native_orders(store, binding, package_id):
    mapping = {}
    for raw, in store.db.execute('SELECT payload FROM demo_instance_outbox ORDER BY rowid'):
        accepted = decode(json.loads(raw))
        receipt = accepted['receipt']
        if receipt['binding'] != binding or receipt['package_id'] != package_id or receipt['decision']['kind'] != 'orders':
            continue
        for offered, intent in zip(receipt['decision']['orders'], accepted['intents'], strict=True):
            key = offered.client_order_id
            if key in mapping and mapping[key] != intent.client_order_id:
                raise SafetyBlocked('native order identity maps to multiple account orders')
            mapping[key] = intent.client_order_id
    return mapping


def grid_context(host, runtime):
    binding = runtime.store.binding()
    executor = host.owner.execution.executor
    funds = host.session.adapter.read_facts().usdt()
    snapshot = executor.capital_policy.risk_snapshot(executor, binding['account_id'],
        official_equity=funds['walletBalance'] + funds['unrealizedProfit'])
    risk = next(value for value in snapshot.strategies if value.capital.strategy_id == binding['instance_id'])
    if not snapshot.reconciled or not risk.complete:
        return None
    holdings = executor.store.holdings(binding['account_id'], binding['instance_id'])
    account, rules = {}, {}
    turnover = D(0)
    for client_id, in host.owner.execution.store.db.execute(
            'SELECT client_id FROM demo_order_owners WHERE instance_id=? AND allocation_id=?',
            (binding['instance_id'], binding['allocation_id'])):
        if client_id in host.session.gateway.state.orders:
            _, notional = host.session.adapter._load(client_id)
            turnover += notional
    for symbol in runtime.strategy.spec.symbols:
        filters = host.session.symbol_context(symbol).rules.filters
        rules[symbol] = dict(step_size=D(filters['LOT_SIZE']['stepSize']),
            tick_size=D(filters['PRICE_FILTER']['tickSize']),
            min_quantity=D(filters['LOT_SIZE']['minQty']), min_notional=D(filters['MIN_NOTIONAL']['notional']))
        basis = sum((position.owned_cost_basis for position in risk.positions
                     if position.instrument_id == symbol), D(0))
        committed = basis + risk.capital.reserved
        available = max(D(0), min(risk.capital.cash - risk.capital.reserved,
            risk.nav - committed, risk.capital.capital - committed))
        account[symbol] = dict(inventory=holdings.get(symbol, D(0)), available=available,
            equity=risk.nav, committed=committed, turnover=turnover, account_id=binding['account_id'],
            instrument_id=symbol, market=Market.BINANCE_USDM, reconciled=True)
    return dict(package_id=runtime.package_id, grid_accounts=account, grid_rules=rules,
        execution=dict(native_to_client=native_orders(host.owner.execution.store, binding, runtime.package_id)))
