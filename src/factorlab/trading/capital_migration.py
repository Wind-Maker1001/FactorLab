"""Atomic adoption of explicitly identified legacy execution journals."""
from decimal import Decimal

from .contracts import Market, TERMINAL, decimal_value, utc
from .portfolio_accounting import OwnedPosition, build_portfolio_report
from .storage import dumps

ZERO = Decimal(0)


def adopt_existing(executor, *, receipt_id, account_id, sleeves, official_equity):
    """Import original order/event identities, not a guessed current cash balance.

    Each sleeve supplies instance_id, logical_strategy, allocation_id, capital,
    initial_cash, initial_inventory (quantity, entry price), owned_symbols,
    created_at, orders (PortfolioExecutor outbox payloads), events and the original
    source_receipt. Original source journals remain authoritative migration evidence.
    """
    if not receipt_id or not sleeves:
        raise ValueError('identified migration and explicit legacy sleeves required')
    decimal_value(official_equity, 'official equity', positive=True)
    store, policy = executor.store, executor.capital_policy
    sleeves = tuple(sleeves)
    payload = dumps(dict(account_id=account_id, sleeves=sleeves))
    with store.transaction():
        previous = store.db.execute('SELECT payload FROM capital_migrations WHERE id=?', (receipt_id,)).fetchone()
        if previous is not None:
            if previous[0] != payload:
                raise ValueError('migration receipt identity changed')
            return
        budget = store.db.execute('SELECT budget,currency FROM capital_accounts WHERE account=?',
                                  (account_id,)).fetchone()
        if budget is None or budget[1] != executor.quote_currency:
            raise ValueError('matching static account policy required before migration')
        total = sum((Decimal(amount) for amount, in store.db.execute(
            'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
        total += sum((sleeve['capital'] for sleeve in sleeves), ZERO)
        if total > Decimal(budget[0]) or total > official_equity:
            raise ValueError('legacy allocations exceed static account or official equity')
        account = executor.adapter.read_account()
        if account.account_id != account_id or not account.reconciled:
            raise ValueError('legacy migration requires a matching reconciled account')
        for sleeve in sleeves:
            owner = sleeve['instance_id']
            capital, cash = sleeve['capital'], sleeve['initial_cash']
            decimal_value(capital, 'legacy allocated capital', positive=True)
            decimal_value(cash, 'legacy initial cash')
            utc(sleeve['created_at'])
            if cash > capital or not sleeve['source_receipt']:
                raise ValueError('original capital and source receipt required')
            if store.allocation(account_id, owner) is not None:
                raise ValueError('legacy owner already has an allocation')
            symbols = tuple(sorted(sleeve['owned_symbols']))
            initial = {symbol: OwnedPosition(quantity, quantity * price)
                       for symbol, (quantity, price) in sleeve['initial_inventory'].items()}
            if set(initial) - set(symbols):
                raise ValueError('initial inventory exceeds explicit legacy ownership')
            orders, events = tuple(sleeve['orders']), tuple(sleeve['events'])
            intents = [order['record'].snapshot.intent for order in orders]
            actions = tuple(sleeve.get('corporate_actions', ()))
            evidence = sleeve['source_receipt']
            if actions and set(evidence.get('corporate_action_ids', ())) != {action['id'] for action in actions}:
                raise ValueError('complete original corporate action identity evidence required')
            if (evidence.get('history_complete') is not True
                    or set(evidence['order_ids']) != {intent.client_order_id for intent in intents}
                    or set(evidence['event_ids']) != {event.event_id for event in events}
                    or any(event.event_time < sleeve['created_at'] for event in events)):
                raise ValueError('complete original journal identity evidence required')
            if any(intent.instrument_id not in symbols for intent in intents):
                raise ValueError('legacy orders exceed explicit symbol ownership')
            report = build_portfolio_report(starting_inventory=initial, intents=intents,
                events=events, reference_prices={order['record'].snapshot.intent.client_order_id:
                    order['reference_price'] for order in orders}, account=account,
                prices={position.instrument_id: position.mark_price for position in account.positions},
                official_equity=official_equity, target_weights={}, quote_currency=executor.quote_currency,
                corporate_actions=actions)
            for order in orders:
                snapshot = order['record'].snapshot
                fills = [fill for fill in report.fills if fill.client_order_id == snapshot.intent.client_order_id]
                if (sum((fill.quantity for fill in fills), ZERO) != snapshot.cumulative_filled_quantity
                        or sum((fill.fee for fill in fills), ZERO) != snapshot.cumulative_fee
                        or sum((fill.notional for fill in fills), ZERO) != order.get('filled_notional', ZERO)):
                    raise ValueError('legacy confirmations do not reproduce original order totals')
            store.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                (account_id, owner, sleeve['allocation_id'], str(capital), str(cash)))
            policy.bind(account_id=account_id, strategy_id=owner, instance_id=owner,
                        allocation_id=sleeve['allocation_id'], logical_strategy=sleeve['logical_strategy'])
            for symbol in symbols:
                quantity = next((position.owned_quantity for position in report.positions
                                 if position.instrument_id == symbol), ZERO)
                initial_quantity, price = sleeve['initial_inventory'].get(symbol, (ZERO, ZERO))
                store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                    (account_id, symbol, owner, str(quantity), str(initial_quantity), str(price)))
            # A frozen direct-order plan never regenerates historical target legs.
            from .portfolio_execution import plan_identity
            session = sleeve['trading_session']
            identity = plan_identity(account_id, owner, session, receipt_id)
            request = dict(account=account_id, strategy=owner, version='legacy', session=session,
                rebalance_id=receipt_id, weights={}, owned=list(symbols), initial=sleeve['initial_inventory'],
                multipliers={}, initial_capital=capital, quantity_deltas=None,
                signal_inputs=sleeve['source_receipt'], direct_orders=[(intent, order['reservation_price'])
                    for intent, order in zip(intents, orders)])
            complete = all(order['record'].snapshot.status in TERMINAL and not order['uncertain'] for order in orders)
            plan = dict(id=identity, request=request, created_at=sleeve['created_at'],
                phase='COMPLETE' if complete else 'WAITING', targets={}, reasons={}, equity=capital)
            store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                              (identity, account_id, owner, session, dumps(plan)))
            for order in orders:
                store.db.execute('INSERT INTO portfolio_orders VALUES (?,?,?)',
                                  (order['record'].snapshot.intent.client_order_id, identity, dumps(order)))
            for action in actions:
                store.db.execute('INSERT INTO portfolio_corporate_actions VALUES (?,?,?,?)',
                    (account_id, owner, action['id'], dumps(action)))
            for event in events:
                existing = store.db.execute('SELECT payload FROM portfolio_confirmations WHERE account=? AND event=?',
                                            (account_id, event.event_id)).fetchone()
                if existing is not None and existing[0] != dumps(event):
                    raise ValueError('conflicting original confirmation identity')
                store.db.execute('INSERT OR IGNORE INTO portfolio_confirmations VALUES (?,?,?)',
                                  (account_id, event.event_id, dumps(event)))
        for sleeve in sleeves:
            for symbol in sleeve['owned_symbols']:
                policy.claim(account_id, sleeve['instance_id'], symbol)
        owned = store.holdings(account_id)
        actual = {position.instrument_id: position.quantity for position in account.positions}
        if any(actual.get(symbol, ZERO) != quantity for symbol, quantity in owned.items()):
            raise ValueError('legacy journals do not reconcile to official inventory')
        current_cash = sum((store.cash(account_id, owner, reserve=False) for owner, in store.db.execute(
            'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
        if current_cash > next(asset.total for asset in account.assets if asset.currency == executor.quote_currency):
            raise ValueError('legacy journals exceed official cash')
        if account.market == Market.BINANCE_USDM:
            from .capital_funding import attribute_funding
            from .capital_margin import attribute_margin
            facts = executor.adapter.margin_facts()
            attribution = attribute_funding(store, account_id, executor.adapter.funding_events(),
                executor.adapter.funding_coverage(), facts.observed_at)
            if not facts.source_complete or not attribution.complete:
                raise ValueError('legacy funding history requires reconciliation')
            if not attribute_margin(store, account_id, facts).complete:
                raise ValueError('legacy margin ownership requires reconciliation')
        store.db.execute('INSERT INTO capital_migrations VALUES (?,?)', (receipt_id, payload))
