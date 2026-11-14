"""Serial delivery of Grid proposals through the existing Demo controller."""
from copy import copy
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import json

from factorlab.strategies.grid_execution import GridAccount, GridExecution
from factorlab.trading import Market, OrderEvent, OrderStatus, OrderType, Side
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.storage import decode, dumps
from factorlab.trading.paper import Quote

from .demo_runtime import MAX_NOTIONAL, MAX_QUANTITY
from .execution import DemoExecutionGateway, DemoExecutionTransport
from .protocol import SafetyBlocked
from .demo_session import DemoSession

ZERO = Decimal(0)


def minimum_quantity(rules, price):
    """Smallest step-aligned quantity meeting both exchange minimums."""
    return (max(rules.min_quantity, rules.min_notional / price) / rules.step_size).to_integral_value(
        rounding=ROUND_CEILING) * rules.step_size


class DemoGridBridge:
    """Serial composition seam; does not grant network execution authority.

    Pending Grid proposals remain reservations, not exchange orders. Commands
    persist strategy inputs in the controller journal so a crash before dispatch
    cannot lose an issued identity. Economic events stay in the existing ledger.
    """

    def __init__(self, grid, controller, *, expect=None, read_only=False, preview=False):
        self.session = controller if isinstance(controller, DemoSession) else None
        self.expect = expect
        self.read_only = read_only
        self.preview = preview
        self.event_cursor = self.turnover_cursor = 0
        self.filled = {}
        self.turnover = ZERO
        if self.session is not None:
            controller = self.session.controller
        if self.session is not None and not read_only and not preview:
            if expect is None or self.session._write_guard is None:
                raise SafetyBlocked('Grid network delivery requires persistent runtime admission')
            plan = self.session.client.transport.authorization.plan
            if (not grid.deployment_id.startswith(plan.client_id_prefix + '-')
                    or len(grid.deployment_id) + 9 > 36):
                raise SafetyBlocked('Grid identity is outside the authorized campaign or length limit')
        gateway = controller.broker
        if not isinstance(gateway, DemoExecutionGateway) or (self.session is None and not isinstance(
                gateway.adapter.client.transport, DemoExecutionTransport)):
            raise SafetyBlocked('Grid network delivery requires a bound Demo session')
        if not isinstance(grid, GridExecution) or (grid.market, grid.account_id, grid.instrument_id) != (
                Market.BINANCE_USDM, gateway.adapter.account_id, 'NVDAUSDT'):
            raise ValueError('Grid Demo identity mismatch')
        if grid.delivery or grid.active:
            raise ValueError('fresh Grid required for journal replay')
        if (Decimal(str(grid.config.max_inventory)) * grid.rules.quantity_unit > MAX_QUANTITY
                or Decimal(str(grid.config.max_cash_committed)) > MAX_NOTIONAL):
            raise SafetyBlocked('Grid configuration exceeds Demo inventory or capital budget')
        filters = gateway.rules.filters
        lot, price = filters['LOT_SIZE'], filters['PRICE_FILTER']
        if (grid.rules.step_size != Decimal(lot['stepSize'])
                or grid.rules.tick_size != Decimal(price['tickSize'])
                or grid.rules.min_quantity != Decimal(lot['minQty'])
                or grid.rules.min_notional != Decimal(filters['MIN_NOTIONAL']['notional'])):
            raise ValueError('Grid rules differ from observed Demo rules')
        self.grid, self.controller, self.gateway = grid, controller, gateway
        self.prefix = 'demo-grid:' + grid.deployment_id + ':'
        self.seen = set()
        self.terminal_reports = set()
        self.ready = False
        saved = controller.store.event_payload(self.prefix + 'deployment')
        binding = grid.deployment()
        if saved is not None and saved != dumps(binding):
            raise ValueError('persisted Grid deployment differs')
        with controller.store.transaction():
            controller.store.record_metadata(self.prefix + 'deployment', binding)
        self.restored = False

    def _restore(self):
        for key, raw in self.controller.store.db.execute('SELECT id,payload FROM events ORDER BY rowid'):
            if key.startswith(self.prefix + 'command:'):
                command = decode(json.loads(raw))
                self._apply(self.grid, command)
                if command['action'] == 'confirm':
                    self._remember(command['value'])
        self.restored = True

    @staticmethod
    def _apply(grid, command):
        action, value = command['action'], command['value']
        if action == 'bar':
            grid.complete_bar(delivery=grid.delivery + 1)
        else:
            account = GridAccount(**command['account'])
            method = grid.withdraw_unsubmitted if action == 'withdraw' else getattr(grid, action)
            method(value, account, delivery=grid.delivery + 1)

    def _command(self, action, value=None, *, source_id=None):
        store = self.controller.store
        source_key = self.prefix + 'source:' + source_id if source_id is not None else None
        if source_key is not None:
            saved = store.event_payload(source_key)
            if saved is not None:
                if saved != dumps(value):
                    raise ValueError('Grid market event identity conflict')
                return False
        command = dict(action=action, value=value,
                       account=asdict(self.account()) if action != 'bar' else None)
        staged = copy(self.grid)
        self._apply(staged, command)
        with store.transaction():
            if source_key is not None:
                store.record_metadata(source_key, value)
            store.record_metadata(self.prefix + 'command:' + str(staged.delivery), command)
        self.grid.__dict__ = staged.__dict__
        if action == 'confirm':
            self._remember(value)
        return True

    @staticmethod
    def _report(event):
        return (event.client_order_id, event.exchange_order_id, event.status,
                event.cumulative_filled_quantity, event.cumulative_fee)

    def _remember(self, event):
        self.seen.add(event.event_id)
        if event.status in TERMINAL:
            self.terminal_reports.add(self._report(event))

    def account(self):
        snapshot = self.controller.account()
        facts = self.gateway.adapter.facts
        funds = facts.usdt()
        positions = snapshot.positions
        if any(p.instrument_id != self.grid.instrument_id or p.quantity < 0 for p in positions):
            raise SafetyBlocked('dedicated long-only NVDA account required')
        inventory = sum((p.quantity for p in positions), ZERO)
        quote = self.gateway.state.quotes[self.grid.instrument_id].price
        committed = sum((p.quantity * max(p.entry_price, p.mark_price, quote) for p in positions), ZERO)
        committed += sum(((o.intent.quantity - o.cumulative_filled_quantity)
                         * max(o.intent.limit_price or quote, quote)
                         for o in snapshot.open_orders if o.intent.side is Side.BUY), ZERO)
        for cursor, event in self._events_after(self.turnover_cursor):
            self.turnover_cursor = cursor
            if not isinstance(event, OrderEvent):
                continue
            previous = self.filled.get(event.client_order_id, ZERO)
            delta = event.cumulative_filled_quantity - previous
            if delta > 0:
                if self.grid._owns(event.client_order_id):
                    self.turnover += delta * event.fill_price
                self.filled[event.client_order_id] = event.cumulative_filled_quantity
        if inventory > MAX_QUANTITY:
            raise SafetyBlocked('confirmed account exceeds Demo budget')
        asset = next(a for a in snapshot.assets if a.currency == 'USDT')
        confirmed_equity = asset.total + sum((p.quantity * (p.mark_price - p.entry_price)
                                             for p in positions), ZERO)
        return GridAccount(inventory, min(asset.available, funds['availableBalance']),
            min(confirmed_equity, funds['walletBalance'] + funds['unrealizedProfit']), committed, self.turnover,
            snapshot.account_id, self.grid.instrument_id, snapshot.market, snapshot.reconciled)

    def recover(self):
        """GET-reconcile first; replay missing confirmations, never resubmit."""
        self.ready = False
        if self.session is None:
            self.gateway.recover(self.controller)
        else:
            self.session.recover()
            book = self.session._fresh_book()
            self.controller.quote(Quote(self.grid.instrument_id, book['askPrice'], book['time_utc']))
        if not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if not self.restored:
            self._restore()
        self._sync()
        if self.grid.pending_bar is not None:
            self._command('bar')
        self.ready = True
        return self.account()

    def _events_after(self, after):
        rows = self.controller.store.db.execute(
            'SELECT rowid,payload FROM events WHERE rowid>? ORDER BY rowid', (after,)).fetchall()
        for cursor, raw in rows:
            event = decode(json.loads(raw))
            yield cursor, event

    def _sync(self):
        for cursor, event in self._events_after(self.event_cursor):
            if (isinstance(event, OrderEvent) and self.grid._owns(event.client_order_id)
                    and event.event_id not in self.seen
                    and self._report(event) not in self.terminal_reports):
                self._command('confirm', event)
            self.event_cursor = cursor

    def withdraw_unsubmitted(self):
        """Withdraw only local reservations; never change an exchange order."""
        keys = [order.intent.client_order_id for order in self.grid.active
                if order.intent.client_order_id not in self.gateway.state.orders]
        if keys:
            self._command('withdraw', keys)

    def observe(self, event, *, dispatch=True, source_id=None):
        if self.read_only:
            raise SafetyBlocked('Grid session is read-only')
        if not self.ready or not self.account().reconciled:
            raise SafetyBlocked('recover Demo account before Grid decisions')
        self._sync()
        accepted = self._command('observe', event, source_id=source_id)
        result = self.dispatch() if dispatch and accepted else None
        if self.grid.pending_bar is not None and self.ready:
            self._command('bar')
            if dispatch and accepted and result is None:
                result = self.dispatch()
        return result

    def dispatch(self):
        """Deliver at most one proposal, without changing its price or quantity."""
        if not self.ready or not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if self.read_only:
            raise SafetyBlocked('Grid session is read-only')
        if self.preview:
            raise SafetyBlocked('Grid preview cannot write orders')
        self._sync()
        known = self.gateway.state.orders
        for key in tuple(self.grid.cancellations):
            if key not in known:
                # Admission withdrawal is not an exchange cancellation or fill.
                event = OrderEvent(key, None, OrderStatus.REJECTED, ZERO, None,
                    self.gateway.clock(), 'demo-grid-local-admission', 'withdraw:' + key)
                self._command('confirm', event)
                continue
            if known[key].snapshot.status not in TERMINAL:
                result = (self.controller.cancel(key) if self.session is None else self.session.cancel(key))
                self.ready = result is not None and not (self.session and self.session.write_uncertain)
                self._sync()
                return result
        if self.controller.account().open_orders:
            return None
        for order in self.grid.active:
            intent = order.intent
            if intent.client_order_id in known:
                continue
            account = self.account()
            quote = self.gateway.state.quotes[intent.instrument_id].price
            if intent.order_type is not OrderType.LIMIT:
                self.ready = False
                raise SafetyBlocked('Demo Grid requires LIMIT orders; delivery paused for manual risk disposition')
            notional = intent.quantity * max(intent.limit_price, quote)
            if (intent.quantity > MAX_QUANTITY or notional > MAX_NOTIONAL
                    or (intent.side is Side.BUY and (account.inventory + intent.quantity > MAX_QUANTITY
                        or account.committed + notional > MAX_NOTIONAL))
                    or (intent.side is Side.SELL and (not intent.reduce_only or intent.quantity > account.inventory))):
                raise SafetyBlocked('Grid intent exceeds Demo budget or confirmed inventory')
            if self.session is None:
                result = self.controller.submit(intent)
            else:
                self.expect(intent)
                result = self.session.submit_intent(intent)
            self.ready = result is not None and not (self.session and self.session.write_uncertain)
            self._sync()
            return result
        return None
