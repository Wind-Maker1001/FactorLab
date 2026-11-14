"""Serial delivery of Grid proposals through the existing Demo controller."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import json

from factorlab.strategies.grid_execution import GridAccount, GridExecution
from factorlab.trading import Market, OrderEvent, OrderStatus, OrderType, Side
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.storage import decode, dumps

from .demo_runtime import MAX_NOTIONAL, MAX_QUANTITY
from .execution import DemoExecutionGateway, DemoExecutionTransport
from .protocol import SafetyBlocked

ZERO = Decimal(0)


def minimum_quantity(rules, price):
    """Smallest step-aligned quantity meeting both exchange minimums."""
    return (max(rules.min_quantity, rules.min_notional / price) / rules.step_size).to_integral_value(
        rounding=ROUND_CEILING) * rules.step_size


class DemoGridBridge:
    """Offline composition seam; does not grant network execution authority.

    Pending Grid proposals remain reservations, not exchange orders. Commands
    persist strategy inputs in the controller journal so a crash before dispatch
    cannot lose an issued identity. Economic events stay in the existing ledger.
    """

    def __init__(self, grid, controller):
        gateway = controller.broker
        if not isinstance(gateway, DemoExecutionGateway) or not isinstance(
                gateway.adapter.client.transport, DemoExecutionTransport):
            raise SafetyBlocked('Grid bridge currently requires offline Demo transport')
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
            getattr(grid, action)(value, account, delivery=grid.delivery + 1)

    def _command(self, action, value=None):
        command = dict(action=action, value=value,
                       account=asdict(self.account()) if action != 'bar' else None)
        staged = deepcopy(self.grid)
        self._apply(staged, command)
        with self.controller.store.transaction():
            self.controller.store.record_metadata(self.prefix + 'command:' + str(staged.delivery), command)
        self.grid.__dict__ = staged.__dict__
        if action == 'confirm':
            self._remember(value)

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
        turnover, filled = ZERO, {}
        for _, event in self.controller.store.order_events():
            previous = filled.get(event.client_order_id, ZERO)
            delta = event.cumulative_filled_quantity - previous
            if delta > 0:
                if self.grid._owns(event.client_order_id):
                    turnover += delta * event.fill_price
                filled[event.client_order_id] = event.cumulative_filled_quantity
        if inventory > MAX_QUANTITY or committed > MAX_NOTIONAL:
            raise SafetyBlocked('confirmed account exceeds Demo budget')
        asset = next(a for a in snapshot.assets if a.currency == 'USDT')
        confirmed_equity = asset.total + sum((p.quantity * (p.mark_price - p.entry_price)
                                             for p in positions), ZERO)
        return GridAccount(inventory, min(asset.available, funds['availableBalance']),
            min(confirmed_equity, funds['walletBalance'] + funds['unrealizedProfit']), committed, turnover,
            snapshot.account_id, self.grid.instrument_id, snapshot.market, snapshot.reconciled)

    def recover(self):
        """GET-reconcile first; replay missing confirmations, never resubmit."""
        self.ready = False
        self.gateway.recover(self.controller)
        if not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if not self.restored:
            self._restore()
        self._sync()
        self.ready = True
        return self.account()

    def _sync(self):
        for _, event in list(self.controller.store.order_events()):
            if (self.grid._owns(event.client_order_id) and event.event_id not in self.seen
                    and self._report(event) not in self.terminal_reports):
                self._command('confirm', event)

    def observe(self, event):
        if not self.ready or not self.account().reconciled:
            raise SafetyBlocked('recover Demo account before Grid decisions')
        self._sync()
        self._command('observe', event)
        if self.grid.pending_bar is not None:
            self._command('bar')
        return self.dispatch()

    def dispatch(self):
        """Deliver at most one proposal, without changing its price or quantity."""
        if not self.ready or not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
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
                result = self.controller.cancel(key)
                self.ready = result is not None
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
                raise SafetyBlocked('Demo Grid requires LIMIT orders')
            notional = intent.quantity * max(intent.limit_price, quote)
            if (intent.quantity > MAX_QUANTITY or notional > MAX_NOTIONAL
                    or (intent.side is Side.BUY and (account.inventory + intent.quantity > MAX_QUANTITY
                        or account.committed + notional > MAX_NOTIONAL))
                    or (intent.side is Side.SELL and (not intent.reduce_only or intent.quantity > account.inventory))):
                raise SafetyBlocked('Grid intent exceeds Demo budget or confirmed inventory')
            result = self.controller.submit(intent)
            self.ready = result is not None
            self._sync()
            return result
        return None
