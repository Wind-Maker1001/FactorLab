"""Export original Demo execution evidence for shared capital adoption."""
import json

from factorlab.trading.storage import decode, dumps
from .protocol import SafetyBlocked


SOURCE_KEY = 'demo:capital-migration:source'


def register_account_history_source(session, *, receipt_id, manifest, sleeves, fee_rate):
    """Persist independently supplied complete original allocation evidence for startup."""
    from decimal import Decimal
    if manifest['account_id'] != session.adapter.account_id or manifest.get('history_complete') is not True:
        raise SafetyBlocked('complete original account allocation manifest required')
    amounts = {sleeve['kind']: sleeve['capital'] for sleeve in sleeves}
    if len(sleeves) != 2 or amounts != {'grid': Decimal(7), 'portfolio': Decimal(20)}:
        raise SafetyBlocked('initial migration requires original 7/20 USDT allocations')
    if not receipt_id or not isinstance(fee_rate, Decimal) or not fee_rate.is_finite() or fee_rate < 0:
        raise SafetyBlocked('original migration identity and explicit fee allowance required')
    if set(manifest['allocations']) != {sleeve['allocation_id'] for sleeve in sleeves}:
        raise SafetyBlocked('complete original account allocation manifest required')
    _export_account_sleeves(session, manifest['allocations'], sleeves)
    session.controller.store.record_metadata(SOURCE_KEY, dict(receipt_id=receipt_id,
        manifest=manifest, sleeves=sleeves, fee_rate=fee_rate))


def account_history_source(session):
    raw = session.controller.store.event_payload(SOURCE_KEY)
    return None if raw is None else decode(json.loads(raw))


def start_account_history(session, executor):
    """Initialize only the attested original capital, then resume the frozen adoption."""
    from decimal import Decimal
    source = account_history_source(session)
    if source is not None:
        executor.capital_policy.configure(account_id=session.adapter.account_id,
            currency='USDT', budget=Decimal(27), fee_rate=source['fee_rate'])
        adopt_account_history(session, executor, receipt_id=source['receipt_id'],
            manifest=source['manifest'], sleeves=source['sleeves'])
    resume_account_history(session, executor, exclude=() if source is None else (source['receipt_id'],))


def adopt_account_history(session, executor, *, receipt_id, manifest, sleeves):
    """Export every original sleeve before the shared executor's atomic adoption."""
    if (executor.adapter.session is not session
            or manifest['account_id'] != session.adapter.account_id):
        raise SafetyBlocked('migration requires the original account execution owner')
    expected = manifest['allocations']
    supplied = [sleeve['allocation_id'] for sleeve in sleeves]
    if (manifest.get('history_complete') is not True or not expected
            or len(supplied) != len(set(supplied)) or set(supplied) != set(expected)):
        raise SafetyBlocked('complete original account allocation manifest required')
    store = session.controller.store
    store.db.execute('CREATE TABLE IF NOT EXISTS demo_capital_migration_exports ('
        'id TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL)')
    source = dumps(dict(manifest=manifest, sleeves=sleeves))
    saved = store.db.execute('SELECT source,payload FROM demo_capital_migration_exports WHERE id=?',
                             (receipt_id,)).fetchone()
    if saved is not None:
        if saved[0] != source:
            raise SafetyBlocked('original migration source identity changed')
        exports = decode(json.loads(saved[1]))
    else:
        exports = _export_account_sleeves(session, expected, sleeves)
        with store.transaction():
            store.db.execute('INSERT OR IGNORE INTO demo_capital_migration_exports VALUES(?,?,?)',
                             (receipt_id, source, dumps(exports)))
            frozen = store.db.execute('SELECT source,payload FROM demo_capital_migration_exports WHERE id=?',
                                      (receipt_id,)).fetchone()
            if frozen != (source, dumps(exports)):
                raise SafetyBlocked('concurrent original migration snapshot differs')
    from .demo_allocations import DemoAllocations
    owners = DemoAllocations(session.controller)
    for sleeve in exports:
        for order in sleeve['orders']:
            intent = order['record'].snapshot.intent
            previous = owners.owner(intent.client_order_id)
            if previous is not None and previous != (sleeve['instance_id'], sleeve['allocation_id'], intent.instrument_id):
                raise SafetyBlocked('original migration order belongs to another instance')
    funds = session.adapter.read_facts().usdt()
    result = executor.adopt_existing(receipt_id=receipt_id, account_id=session.adapter.account_id,
        sleeves=exports, official_equity=funds['walletBalance'] + funds['unrealizedProfit'])
    with store.transaction():
        for sleeve in exports:
            for order in sleeve['orders']:
                owners.claim_in_transaction(order['record'].snapshot.intent,
                    instance_id=sleeve['instance_id'], allocation_id=sleeve['allocation_id'])
    return result


def resume_account_history(session, executor, *, exclude=()):
    """Resume frozen account migrations before qualifying restored strategy instances."""
    store = session.controller.store
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='demo_capital_migration_exports'").fetchone() is None:
        return
    for receipt_id, raw in store.db.execute(
            'SELECT id,source FROM demo_capital_migration_exports ORDER BY rowid').fetchall():
        if receipt_id in exclude:
            continue
        source = decode(json.loads(raw))
        adopt_account_history(session, executor, receipt_id=receipt_id,
                              manifest=source['manifest'], sleeves=source['sleeves'])


def _export_account_sleeves(session, expected, sleeves):
    from decimal import Decimal
    exports = []
    for sleeve in sleeves:
        values = dict(sleeve)
        kind = values.pop('kind')
        original = expected[values['allocation_id']]
        if any(values[field] != original[field] for field in
               ('instance_id', 'logical_strategy', 'capital', 'initial_cash', 'initial_inventory')):
            raise SafetyBlocked('original allocation economic identity differs')
        if kind == 'portfolio':
            exports.append(export_portfolio_sleeve(session, **values))
        elif kind == 'grid':
            exports.append(export_grid_sleeve(session, **values))
        else:
            raise SafetyBlocked('unknown original allocation source')
    if sum((sleeve['capital'] for sleeve in exports), Decimal(0)) > Decimal(27):
        raise SafetyBlocked('original account allocation exceeds 7/20 USDT capital')
    return exports


def export_grid_sleeve(session, *, source_receipt, order_ids, **allocation):
    """Trace original grid orders through every declared deployment's confirmations."""
    store = session.controller.store
    deployments = [(key, decode(json.loads(raw))) for key, raw in store.db.execute(
        "SELECT id,payload FROM events WHERE id LIKE 'demo-grid:%:deployment' ORDER BY rowid")]
    expected = source_receipt.get('deployment_journal_ids')
    if (expected is None or not deployments or len(expected) != len(set(expected))
            or set(expected) != {key for key, _ in deployments}):
        raise SafetyBlocked('complete original grid deployment manifest required')
    confirmed = set()
    for key, deployment in deployments:
        if (deployment['account_id'] != session.adapter.account_id
                or deployment['instrument_id'] not in allocation['owned_symbols']):
            raise SafetyBlocked('original grid deployment binding differs')
        prefix = key.removesuffix('deployment') + 'command:'
        for raw, in store.db.execute('SELECT payload FROM events WHERE id LIKE ?', (prefix + '%',)):
            command = decode(json.loads(raw))
            if command['action'] == 'confirm':
                client_id = command['value'].client_order_id
                if client_id in session.gateway.state.orders:
                    confirmed.add(client_id)
    if confirmed != set(order_ids):
        raise SafetyBlocked('original grid confirmation history differs from order manifest')
    return export_sleeve(session, source_receipt=source_receipt, order_ids=order_ids, **allocation)


def export_portfolio_sleeve(session, *, instance_id, allocation_id, source_receipt, **allocation):
    """Bind the full original task journal to an independently supplied history manifest."""
    from .demo_allocations import DemoAllocations
    owners = DemoAllocations(session.controller)
    tasks = [(key, decode(json.loads(raw))) for key, raw in session.controller.store.db.execute(
        "SELECT id,payload FROM events WHERE id LIKE 'portfolio:%' ORDER BY rowid")
        if key.count(':') == 1 and decode(json.loads(raw))['allocation'] == allocation_id]
    expected = source_receipt.get('task_journal_ids')
    if (expected is None or len(expected) != len(set(expected))
            or set(expected) != {key for key, _ in tasks} or not tasks):
        raise SafetyBlocked('complete original portfolio task manifest required')
    intents = [intent for key, _ in tasks for intent in owners._portfolio_intents(key, allocation_id)]
    symbols = set(allocation['owned_symbols'])
    if any(set(target['owned']) != symbols for _, target in tasks):
        raise SafetyBlocked('original portfolio symbol ownership changed')
    return export_sleeve(session, instance_id=instance_id, allocation_id=allocation_id,
        source_receipt=source_receipt, order_ids=[intent.client_order_id for intent in intents], **allocation)


def export_sleeve(session, *, instance_id, logical_strategy, allocation_id, capital,
                  initial_cash, initial_inventory, owned_symbols, created_at,
                  trading_session, order_ids, source_receipt):
    """Keep original orders, confirmations and source evidence; never infer a cash plug."""
    if source_receipt.get('history_complete') is not True:
        raise SafetyBlocked('complete original allocation history required for migration')
    if not session.gateway.read_account().reconciled:
        raise SafetyBlocked('reconcile original Demo account before migration')
    order_ids = tuple(order_ids)
    identities = set(order_ids)
    if len(identities) != len(order_ids):
        raise SafetyBlocked('duplicate original migration order identity')
    events = [event for _, event in session.controller.store.order_events()
              if event.client_order_id in identities]
    expected_orders = source_receipt.get('order_ids')
    expected_events = source_receipt.get('event_ids')
    if (expected_orders is None or expected_events is None
            or len(expected_orders) != len(set(expected_orders))
            or len(expected_events) != len(set(expected_events))
            or set(expected_orders) != identities
            or set(expected_events) != {event.event_id for event in events}):
        raise SafetyBlocked('original migration identity manifest differs from available history')
    orders = []
    for client_id in order_ids:
        local = session.gateway.state.orders[client_id]
        remote, filled_notional = session.adapter._load(client_id)
        if (local.snapshot != remote.snapshot or local.snapshot.intent.instrument_id not in owned_symbols):
            raise SafetyBlocked('legacy order differs from confirmed account journal')
        if not any(event.client_order_id == client_id for event in events):
            raise SafetyBlocked('legacy order has no original confirmation history')
        reservation = local.reservation_price
        orders.append(dict(record=decode(json.loads(dumps(local))),
            reference_price=reservation, reservation_price=reservation,
            filled_notional=filled_notional, attempted=True,
            uncertain=local.snapshot.reconcile_required))
    coverage = session.adapter.funding_coverage()
    if coverage is None or not coverage['complete']:
        raise SafetyBlocked('complete original funding coverage required for migration')
    evidence = dict(source_receipt, order_ids=list(order_ids),
                    event_ids=[event.event_id for event in events], funding_coverage=coverage)
    return dict(instance_id=instance_id, logical_strategy=logical_strategy,
        allocation_id=allocation_id, capital=capital, initial_cash=initial_cash,
        initial_inventory=initial_inventory, owned_symbols=sorted(owned_symbols),
        created_at=created_at, trading_session=trading_session, orders=orders,
        events=events, source_receipt=evidence)
