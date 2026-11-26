"""V2 decisions enter the existing Execute owner's durable outbox."""
from decimal import Decimal as D, ROUND_DOWN
import json

from factorlab.trading import Market, OrderIntent, OrderType, Side
from factorlab.trading.realtime import identity
from factorlab.trading.storage import decode, dumps
from .demo_allocations import DemoAllocations
from .protocol import SafetyBlocked


class DemoInstanceExecutionAdapter:
    def __init__(self, session, *, executor=None):
        self.session = session
        self.executor = executor
        self.store = session.controller.store
        if executor is not None:
            session.controller.demo_portfolio_store = executor.store
            session.controller.demo_account_id = session.adapter.account_id
        self.owners = DemoAllocations(session.controller)
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS demo_instance_outbox (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0)''')

    def qualify(self, spec, binding, allocation):
        if binding['market'] != Market.BINANCE_USDM.value or binding['account_id'] != self.session.adapter.account_id:
            raise SafetyBlocked('Demo instance market/account mismatch')
        if self.executor is None or getattr(self.executor, 'capital_policy', None) is None:
            raise SafetyBlocked('new Demo instances require the shared capital admission policy')
        if spec.capital['currency'] != 'USDT':
            raise SafetyBlocked('Demo instance capital must be denominated in USDT')
        if spec.execution['shorting']:
            raise SafetyBlocked('existing cash-funded PortfolioExecutor does not support shorting')
        if (set(spec.execution['order_types']) - {'LIMIT'}
                or set(spec.execution['time_in_force']) - {'GTC'}):
            raise SafetyBlocked('Demo instance supports LIMIT GTC only')
        for symbol in spec.symbols:
            self.session.symbol_context(symbol)
        funded = self.executor.capital_policy.qualify(spec, binding, allocation)
        allocation = dict(instance_id=binding['instance_id'],
                          allocation_id=funded['allocation_id'], budget_usdt=funded['capital'])
        self.store.record_metadata('demo:instance:' + binding['instance_id'],
            dict(binding=binding, symbols=sorted(spec.symbols), allocation=allocation,
                 shorting=spec.execution['shorting']))

    def prepare_plan(self, decision_id, *, now):
        """Recover the same shared execution plan after durable receipt acceptance."""
        if self.executor is None:
            raise SafetyBlocked('shared PortfolioExecutor required')
        row = self.store.db.execute(
            'SELECT payload FROM demo_instance_outbox WHERE id=?', (decision_id,)).fetchone()
        if row is None:
            raise SafetyBlocked('unknown accepted Demo decision')
        accepted = decode(json.loads(row[0]))
        receipt = accepted['receipt']
        binding = receipt['binding']
        funded = self.executor.store.allocation(binding['account_id'], binding['instance_id'])
        if funded is None or funded['allocation_id'] != binding['allocation_id']:
            raise SafetyBlocked('instance requires its existing shared capital allocation')
        configured = decode(json.loads(self.store.event_payload('demo:instance:' + binding['instance_id'])))
        return self.executor.create(
            account_id=binding['account_id'], strategy_id=binding['instance_id'],
            strategy_version=receipt['package_id'], trading_session=receipt['event_id'],
            weights={}, owned_symbols=configured['symbols'], now=now,
            rebalance_id=decision_id, signal_inputs=dict(input_identity=receipt['input_identity']),
            direct_orders=[(intent, intent.limit_price) for intent in accepted['intents']],
            concurrent_direct_orders=receipt['decision']['kind'] == 'orders')

    def advance(self, decision_id, *, equity, quotes, rules, now):
        """Advance one accepted decision on the account owner's execution turn."""
        accepted = decode(json.loads(self.store.db.execute(
            'SELECT payload FROM demo_instance_outbox WHERE id=?', (decision_id,)).fetchone()[0]))
        from factorlab.trading.contracts import TERMINAL
        binding = accepted['receipt']['binding']
        for cancellation in accepted['cancellations']:
            client_id = cancellation['client_order_id']
            self.owners.require_cancel(client_id, instance_id=binding['instance_id'],
                                       allocation_id=binding['allocation_id'])
            row = self.executor.store.db.execute(
                'SELECT plan FROM portfolio_orders WHERE id=?', (client_id,)).fetchone()
            if row is None:
                original = next(key for key, raw in self.store.db.execute(
                    'SELECT id,payload FROM demo_instance_outbox ORDER BY rowid')
                    if any(intent.client_order_id == client_id
                           for intent in decode(json.loads(raw))['intents']))
                original_plan = self.prepare_plan(original, now=now)
            else:
                original_plan = row[0]
            self.executor.cancel(original_plan, client_id)
            order = next(order for order in self.executor.store.orders(original_plan)
                if order['record'].snapshot.intent.client_order_id == client_id)
            if order['record'].snapshot.status not in TERMINAL or order['uncertain']:
                return None
        plan_id = self.prepare_plan(decision_id, now=now)
        from dataclasses import replace
        execution_quotes = {symbol: replace(quote, session=accepted['receipt']['event_id'])
                            for symbol, quote in quotes.items()}
        result = self.executor.run_once(plan_id, equity=equity, quotes=execution_quotes, rules=rules, now=now)
        if result.phase == 'COMPLETE':
            with self.store.transaction():
                self.store.db.execute('UPDATE demo_instance_outbox SET completed=1 WHERE id=?',
                                      (decision_id,))
        return result

    def execute(self, receipt):
        binding = receipt['binding']
        instance = binding['instance_id']
        raw = self.store.event_payload('demo:instance:' + instance)
        if raw is None:
            raise SafetyBlocked('Demo instance is not qualified')
        configured = decode(json.loads(raw))
        if configured['binding'] != binding:
            raise SafetyBlocked('Demo instance binding mismatch')
        decision = receipt['decision']
        key = 'demo:decision:' + identity((instance, receipt['package_id'], receipt['event_id']))
        existing = self.store.db.execute('SELECT payload FROM demo_instance_outbox WHERE id=?', (key,)).fetchone()
        if existing is not None:
            saved = decode(json.loads(existing[0]))
            if saved['receipt'] != receipt:
                raise SafetyBlocked('decision identity has conflicting content')
            return
        for pending, in self.store.db.execute('SELECT payload FROM demo_instance_outbox WHERE completed=0'):
            previous = decode(json.loads(pending))['receipt']
            if (previous['binding']['instance_id'] == instance
                    and (decision['kind'] != 'orders' or previous['decision']['kind'] != 'orders')):
                raise SafetyBlocked('instance must reconcile its previous decision before accepting another')
        symbols = set(configured['symbols'])
        intents = []
        if decision['kind'] == 'orders':
            from .demo_grid_context import native_orders
            existing_native = native_orders(self.store, binding, receipt['package_id'])
            for index, offered in enumerate(decision['orders']):
                if offered.client_order_id in existing_native:
                    raise SafetyBlocked('native order was already accepted by this instance package')
                if offered.instrument_id not in symbols or offered.market is not Market.BINANCE_USDM:
                    raise SafetyBlocked('order outside instance symbol declaration')
                if not configured['shorting'] and offered.side is Side.SELL and not offered.reduce_only:
                    raise SafetyBlocked('instance has not declared shorting')
                from dataclasses import replace
                intents.append(replace(offered, client_order_id='di' + identity((key, index))[:30]))
        else:
            for symbol, target in sorted(decision['targets'].items()):
                if symbol not in symbols:
                    raise SafetyBlocked('target outside instance symbol declaration')
                context = self.session.symbol_context(symbol)
                book = self.session._fresh_book(symbol=symbol)
                own = self.owners.inventory(instance, symbol)
                kind = decision['kind']
                if kind in {'target_weight', 'target_weights'}:
                    if receipt['package_id'] == 'mailbox-target-v1':
                        capital = D(receipt['event']['task']['equity_usdt'])
                    else:
                        capital = self.executor.store.allocation(binding['account_id'], instance)['capital']
                    target = capital * target / book['askPrice']
                elif kind == 'quantity_deltas':
                    target += own
                elif kind != 'target_quantities':
                    raise SafetyBlocked('unsupported instance decision kind')
                if target < 0 and not configured['shorting']:
                    raise SafetyBlocked('instance has not declared shorting')
                step = D(context.rules.filters['LOT_SIZE']['stepSize'])
                target = (target / step).to_integral_value(rounding=ROUND_DOWN) * step
                delta = target - own
                if not delta:
                    continue
                if own and target * own < 0:
                    raise SafetyBlocked('flatten owned inventory before reversing direction')
                side = Side.BUY if delta > 0 else Side.SELL
                reduction = bool(own and delta * own < 0)
                price = self.session.limit_price(context.rules, side, book)
                intents.append(OrderIntent(Market.BINANCE_USDM, symbol, side, OrderType.LIMIT,
                    abs(delta), price, 'di' + identity((key, symbol))[:30],
                    position_side='BOTH', reduce_only=reduction))
        cancellations = decision['cancellations']
        for cancel in cancellations:
            if cancel['symbol'] not in symbols:
                raise SafetyBlocked('cancellation outside instance symbol declaration')
            self.owners.require_cancel(cancel['client_order_id'], instance_id=instance,
                                       allocation_id=binding['allocation_id'])
        # Every child is accepted atomically; replay never re-applies a quantity delta.
        with self.store.transaction():
            for intent in intents:
                self.owners.claim_in_transaction(intent, instance_id=instance, allocation_id=binding['allocation_id'])
            self.store.db.execute('INSERT INTO demo_instance_outbox(id,payload) VALUES(?,?)',
                (key, dumps(dict(receipt=receipt, intents=intents,
                    cancellations=cancellations, allocation=configured['allocation']))))
