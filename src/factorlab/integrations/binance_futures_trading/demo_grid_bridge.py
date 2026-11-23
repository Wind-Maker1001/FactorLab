"""Serial delivery of Grid proposals through the existing Demo controller."""
from copy import copy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json

from factorlab.strategies.grid_execution import GridAccount, GridExecution
from factorlab.trading import Market, OrderEvent, OrderStatus, OrderType, Side
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.control import RiskRejected
from factorlab.trading.storage import decode, dumps
from factorlab.trading.paper import Quote

from .demo_preflight import _book
from .demo_runtime import MAX_NOTIONAL, MAX_QUANTITY
from .execution import DemoExecutionGateway, DemoExecutionTransport
from .protocol import SafetyBlocked
from .demo_session import DemoSession
from .rules import common_step

ZERO = Decimal(0)
EXIT_REPRICE_SECONDS = 180
EXIT_MAX_ATTEMPTS = 5


def _classify_exit_block(error):
    text = str(error)
    if 'price band' in text or 'mark band' in text:
        return 'EXIT_PRICE_BAND'
    if 'plan notional limit' in text or 'plan margin limit' in text:
        return 'EXIT_MAX_NOTIONAL'
    if 'global Demo trading budget exhausted' in text or 'plan total order limit' in text:
        return 'EXIT_GLOBAL_BUDGET_EXHAUSTED'
    if 'persistent Demo POST budget exhausted' in text:
        return 'EXIT_WINDOW_BUDGET_EXHAUSTED'
    if 'PRICE_FILTER' in text:
        return 'EXIT_PRICE_FILTER'
    if 'QUANTITY_FILTER' in text:
        return 'EXIT_MIN_QUANTITY'
    if 'MIN_NOTIONAL' in text:
        return 'EXIT_MIN_NOTIONAL'
    if 'RISK_BRACKET_LIMIT' in text:
        return 'EXIT_RULE_REJECTED'
    return None


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

    def __init__(self, grid, controller, *, expect=None, read_only=False, preview=False,
                 deferred_admission=False, expected_prefix=None, new_deployment=None):
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
            if expect is None or (self.session._write_guard is None and not deferred_admission):
                raise SafetyBlocked('Grid network delivery requires persistent runtime admission')
            if self.session._write_guard is not None:
                prefix = self.session.client.transport.authorization.plan.client_id_prefix
            else:
                prefix = expected_prefix
            if (not prefix or not grid.deployment_id.startswith(prefix + '-')
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
        self.grid, self.controller, self.gateway = grid, controller, gateway
        self.prefix = 'demo-grid:' + grid.deployment_id + ':'
        self.seen = set()
        self.terminal_reports = set()
        self.ready = False
        self.last_book = None
        self.exit_reprice_seconds = EXIT_REPRICE_SECONDS
        self.exit_max_attempts = EXIT_MAX_ATTEMPTS
        self.exit_block_reason = None
        self._exit_awaiting_reconcile = False
        saved = controller.store.event_payload(self.prefix + 'deployment')
        binding = grid.deployment()
        if saved is not None and saved != dumps(binding):
            raise ValueError('persisted Grid deployment differs')
        if saved is None:
            filters = gateway.rules.filters
            lot, price = filters['LOT_SIZE'], filters['PRICE_FILTER']
            if (grid.rules.step_size != Decimal(lot['stepSize'])
                    or grid.rules.tick_size != Decimal(price['tickSize'])
                    or grid.rules.min_quantity != Decimal(lot['minQty'])
                    or grid.rules.min_notional != Decimal(filters['MIN_NOTIONAL']['notional'])):
                raise ValueError('Grid rules differ from observed Demo rules')
        with controller.store.transaction():
            controller.store.record_metadata(self.prefix + 'deployment', binding)
            if new_deployment is not None:
                controller.store.record_metadata('demo-grid:deployment:' + grid.deployment_id, new_deployment)
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
            if action == 'withdraw':
                method = grid.withdraw_unsubmitted
            elif action == 'exit_limit':
                method = grid.propose_limit_exit
            elif action == 'exit_cancel':
                method = grid.cancel_exit
            else:
                method = getattr(grid, action)
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

    def recover(self, *, collection=None):
        """GET-reconcile first; replay missing confirmations, never resubmit."""
        self.ready = False
        if self.session is None:
            self.gateway.recover(self.controller)
        else:
            self.session.recover()
            if collection is None:
                book = self.session._fresh_book()
            else:
                previous = self.gateway.state.quotes.get(self.grid.instrument_id)
                market_after = 0 if previous is None else round(previous.observed_at.timestamp() * 1000)
                receipt = collection.receive_recent(collection.clock(), market_after=market_after)
                book = self.session._fresh_book(dict(receipt.payload))
            self.last_book = book
            self.controller.quote(Quote(self.grid.instrument_id, book['askPrice'], book['time_utc']))
        return self.restore_reconciled()

    def restore_reconciled(self):
        """Project the Grid over an already GET-reconciled shared account."""
        if not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if not self.restored:
            self._restore()
        self._sync()
        if self.grid.pending_bar is not None:
            self._command('bar')
        self._exit_awaiting_reconcile = False
        if not self.grid.closed_by_breakout or self.account().inventory == ZERO:
            self.exit_block_reason = None
        self.ready = True
        return self.account()

    def _protocol_order_ids(self):
        return {row[0] for row in self.gateway.adapter.db.execute('SELECT id FROM futures_orders')}

    def _delete_already_consumed(self, client_order_id):
        runtime = getattr(self, 'runtime', None)
        if runtime is None:
            return False
        return runtime.db.execute(
            "SELECT 1 FROM dispatches WHERE method='DELETE' AND client_id=?",
            (client_order_id,),
        ).fetchone() is not None

    def _has_unconfirmed_dispatch(self):
        runtime = getattr(self, 'runtime', None)
        if runtime is None:
            return False
        for (client_id,) in runtime.db.execute('SELECT client_id FROM dispatches WHERE receipt IS NULL'):
            order = self.gateway.state.orders.get(client_id)
            if order is None or order.snapshot.status not in TERMINAL:
                return True
        return False

    def _dispatched_exit_attempts(self):
        sent = set(self.gateway.state.orders) | self._protocol_order_ids()
        return sum(1 for key in self.grid.exit_orders if key in sent)

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
        sent = set(self.gateway.state.orders) | self._protocol_order_ids()
        keys = [order.intent.client_order_id for order in self.grid.active
                if order.intent.client_order_id not in sent]
        if keys:
            self._command('withdraw', keys)

    def _fresh_exit_book(self, book=None):
        now = self.gateway.clock()
        facts = self.gateway.adapter.facts
        if facts is None:
            raise SafetyBlocked('STALE_ACCOUNT: Demo exit requires account evidence')
        self.gateway.adapter._require_fresh_account_read(
            facts.observed_at, facts.read_started_monotonic, now)
        previous = self.gateway.state.quotes.get(self.grid.instrument_id)

        def usable(candidate):
            if candidate is None:
                return None
            parsed = candidate if 'time_utc' in candidate else _book(dict(candidate))
            if (not timedelta(0) <= now - parsed['time_utc'] <= timedelta(seconds=5)
                    or parsed['bidPrice'] <= 0 or parsed['bidPrice'] > parsed['askPrice']):
                return None
            if previous is not None and parsed['time_utc'] < previous.observed_at:
                return None
            return parsed

        for candidate in (book, self.last_book):
            parsed = usable(candidate)
            if parsed is not None:
                self.last_book = parsed
                return parsed
        if self.session is not None:
            fresh = self.session._fresh_book()
            if previous is not None and fresh['time_utc'] < previous.observed_at:
                raise SafetyBlocked('STALE_QUOTE: Demo exit book regression')
            self.last_book = fresh
            return fresh
        if previous is None or not timedelta(0) <= now - previous.observed_at <= timedelta(seconds=5):
            raise SafetyBlocked('STALE_QUOTE: Demo exit book')
        return dict(bidPrice=previous.price, askPrice=previous.price, time_utc=previous.observed_at)

    def _plan_limit_exit(self, inventory, book):
        filters = self.gateway.rules.filters
        price_filter = filters.get('PRICE_FILTER') or {}
        tick = Decimal(price_filter['tickSize'])
        if tick <= 0:
            return None, None, 'EXIT_PRICE_FILTER'
        # The exit must satisfy both current exchange filters and the saved Grid deployment.
        tick = common_step(tick, self.grid.rules.tick_size)
        price = (book['bidPrice'] / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        min_price = Decimal(price_filter['minPrice']) if price_filter.get('minPrice') is not None else ZERO
        max_price = Decimal(price_filter['maxPrice']) if price_filter.get('maxPrice') is not None else ZERO
        if (price <= 0 or price < Decimal(str(self.grid.config.minimum_price))
                or (min_price and price < min_price) or (max_price and price > max_price)
                or price % tick):
            return None, None, 'EXIT_PRICE_FILTER'

        lot = filters.get('LOT_SIZE') or {}
        step = common_step(Decimal(lot['stepSize']), self.grid.rules.step_size)
        min_qty = max(self.grid.rules.min_quantity, Decimal(lot['minQty']))
        max_qty = min(MAX_QUANTITY, Decimal(lot['maxQty']))
        min_notional = max(self.grid.rules.min_notional, Decimal(filters['MIN_NOTIONAL']['notional']))
        available_qty = (inventory / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if available_qty <= 0 or available_qty < min_qty:
            return None, None, 'EXIT_MIN_QUANTITY'
        if available_qty * price < min_notional:
            return None, None, 'EXIT_MIN_NOTIONAL'

        quote = self.gateway.state.quotes[self.grid.instrument_id].price
        quote_price = max(price, book['askPrice'], quote)
        max_by_notional = (MAX_NOTIONAL / quote_price / step).to_integral_value(rounding=ROUND_FLOOR) * step
        cap = min(max_qty, max_by_notional)
        if available_qty <= cap:
            quantity = available_qty
        else:
            minimum = (max(min_qty, min_notional / price) / step).to_integral_value(rounding=ROUND_CEILING) * step
            if cap < minimum:
                return None, None, 'EXIT_MAX_NOTIONAL' if max_by_notional < minimum else 'EXIT_MAX_QUANTITY'
            chunks = (available_qty / cap).to_integral_value(rounding=ROUND_CEILING)
            quantity = (min(cap, available_qty - (chunks - 1) * minimum) / step).to_integral_value(
                rounding=ROUND_FLOOR) * step
            if quantity < minimum:
                return None, None, 'EXIT_MAX_NOTIONAL'

        if quantity <= 0 or quantity > inventory or quantity < min_qty or quantity > max_qty:
            return None, None, 'EXIT_MIN_QUANTITY'
        if quantity * price < min_notional:
            return None, None, 'EXIT_MIN_NOTIONAL'
        if quantity * quote_price > MAX_NOTIONAL:
            return None, None, 'EXIT_MAX_NOTIONAL'
        if self._dispatched_exit_attempts() >= self.exit_max_attempts:
            return None, None, 'EXIT_RETRY_LIMIT_EXCEEDED'

        runtime = getattr(self, 'runtime', None)
        if runtime is not None:
            continuous = runtime.continuous_authorization()
            campaign = runtime.active_campaign()
            if continuous is not None:
                if continuous['state'] == 'HALTED' or runtime._get('continuous_diagnostic') is not None:
                    return None, None, 'HALTED'
                if continuous['state'] != 'RUNNING':
                    return None, None, continuous['state']
                funds = self.gateway.adapter.facts.usdt()
                loss = Decimal(continuous['opening_equity']) - (funds['walletBalance'] + funds['unrealizedProfit'])
                if loss >= Decimal(continuous['max_loss']):
                    return None, None, 'EXIT_LOSS_BUDGET_EXHAUSTED'
                consumed = runtime.consumption(continuous)
                if (consumed['posts'] >= continuous['total_orders']
                        or Decimal(consumed['submitted_notional']) + quantity * price
                        > Decimal(continuous['total_notional'])):
                    return None, None, 'EXIT_GLOBAL_BUDGET_EXHAUSTED'
            if campaign is None or campaign['smoke']:
                return None, None, 'AUTHORIZATION_REQUIRED'
            if runtime.post_count(campaign['id']) >= campaign['orders']:
                return None, None, 'EXIT_WINDOW_BUDGET_EXHAUSTED'
        return price, quantity, None

    def breakout_exit_status(self, *, desired='RUNNING', authorized=True):
        """Return bounded breakout exit lifecycle diagnostics, or None outside breakout."""
        if not self.grid.closed_by_breakout:
            return None
        facts = self.gateway.adapter.facts
        attempts = self._dispatched_exit_attempts()
        status = self.session.status() if self.session is not None else None
        unknown = (status['unknown'] if status is not None
                   else [k for k, r in self.gateway.state.orders.items() if r.snapshot.reconcile_required])
        uncertain = bool(self.session is not None and (self.session.write_uncertain or self.session.receipt_error))
        runtime = getattr(self, 'runtime', None)
        pending_recovery = bool(runtime is not None and runtime._get('continuous_recovery'))
        if (facts is None or not self.ready or unknown or uncertain or pending_recovery
                or self._exit_awaiting_reconcile or self._has_unconfirmed_dispatch()):
            reason = ('ORDER_UNKNOWN' if unknown else
                      'WRITE_UNCERTAIN' if uncertain or pending_recovery else 'RECONCILE_REQUIRED')
            return dict(phase='EXIT_RECONCILING', reason=reason, entry_blocked=True,
                        inventory=None if facts is None else sum((p.quantity for p in facts.shared().positions), ZERO),
                        active_exit_order_id=None, attempts=attempts, max_attempts=self.exit_max_attempts)
        reconciled = bool(status['reconciled'] if status is not None else self.controller.account().reconciled)
        if not reconciled:
            return dict(phase='EXIT_RECONCILING', reason='RECONCILE_REQUIRED', entry_blocked=True,
                        inventory=None, active_exit_order_id=None,
                        attempts=attempts, max_attempts=self.exit_max_attempts)
        account = self.account()
        inventory = account.inventory
        open_orders = self.controller.account().open_orders
        open_buys = [o for o in open_orders if o.intent.side is Side.BUY]
        open_sells = [o for o in open_orders if o.intent.side is Side.SELL and o.intent.reduce_only]
        active_exit_id = open_sells[0].intent.client_order_id if open_sells else None
        if inventory == ZERO and not open_orders:
            phase, reason = 'FLAT_CONFIRMED', None
        elif open_buys:
            if desired != 'RUNNING':
                phase, reason = 'EXIT_BLOCKED', 'USER_PAUSED' if desired == 'PAUSED' else desired
            elif self.read_only or self.preview:
                phase, reason = 'EXIT_BLOCKED', 'PREVIEW_OR_READ_ONLY'
            elif not authorized:
                phase, reason = 'EXIT_BLOCKED', 'AUTHORIZATION_REQUIRED'
            else:
                phase, reason = 'ENTRY_BLOCKED', self.exit_block_reason or 'CANCEL_OPEN_BUY'
        elif inventory > ZERO and open_sells:
            phase = 'EXIT_WORKING'
            if desired == 'PAUSED':
                reason = 'USER_PAUSED'
            elif desired in {'STOPPED', 'HALTED'}:
                reason = desired
            else:
                reason = self.exit_block_reason
        elif inventory > ZERO:
            if desired in {'STOPPED', 'HALTED'}:
                phase, reason = 'EXIT_BLOCKED', desired
            elif desired == 'PAUSED':
                phase, reason = 'EXIT_BLOCKED', 'USER_PAUSED'
            elif self.read_only or self.preview:
                phase, reason = 'EXIT_BLOCKED', 'PREVIEW_OR_READ_ONLY'
            elif self.exit_block_reason is not None:
                phase, reason = 'EXIT_BLOCKED', self.exit_block_reason
            elif not authorized:
                phase, reason = 'EXIT_BLOCKED', 'AUTHORIZATION_REQUIRED'
            else:
                phase, reason = 'EXIT_PENDING', 'BREAKOUT_POSITION_EXIT'
        else:
            phase, reason = 'EXIT_RECONCILING', 'OPEN_ORDER_RECONCILE'
        return dict(phase=phase, reason=reason, entry_blocked=True, inventory=inventory,
                    active_exit_order_id=active_exit_id, attempts=attempts, max_attempts=self.exit_max_attempts)

    def step_breakout_exit(self, *, book=None):
        """Advance breakout reduce-only LIMIT exit by at most one exchange action."""
        if not self.grid.closed_by_breakout or self.read_only or self.preview:
            return None
        if not self.ready or not self.account().reconciled:
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if self._exit_awaiting_reconcile:
            return None
        if self.session is not None and (self.session.write_uncertain or self.session.receipt_error):
            raise SafetyBlocked('RECONCILE_REQUIRED')
        runtime = getattr(self, 'runtime', None)
        if runtime is not None and runtime._get('continuous_recovery'):
            raise SafetyBlocked('RECONCILE_REQUIRED')
        if self._has_unconfirmed_dispatch():
            raise SafetyBlocked('RECONCILE_REQUIRED')
        self._sync()
        if any(r.snapshot.reconcile_required for r in self.gateway.state.orders.values()):
            raise SafetyBlocked('RECONCILE_REQUIRED')

        self.withdraw_unsubmitted()
        open_orders = self.controller.account().open_orders

        # Cancel any resting BUY order first so entry stays blocked and the single open-order slot is freed.
        for order in open_orders:
            if order.intent.side is Side.BUY:
                key = order.intent.client_order_id
                if self._delete_already_consumed(key):
                    self.exit_block_reason = 'EXIT_CANCEL_ALREADY_CONSUMED'
                    return None
                result = self.controller.cancel(key) if self.session is None else self.session.cancel(key)
                self._exit_awaiting_reconcile = True
                self.ready = result is not None and not (self.session and self.session.write_uncertain)
                self._sync()
                return result

        account = self.account()
        if account.inventory == ZERO:
            self.exit_block_reason = None
            for order in open_orders:
                key = order.intent.client_order_id
                if self._delete_already_consumed(key):
                    self.exit_block_reason = 'EXIT_CANCEL_ALREADY_CONSUMED'
                    return None
                result = self.controller.cancel(key) if self.session is None else self.session.cancel(key)
                self._exit_awaiting_reconcile = True
                self.ready = result is not None and not (self.session and self.session.write_uncertain)
                self._sync()
                return result
            return None

        if self.session is not None:
            self.session.refresh_rules()

        # Existing working reduce-only SELL: monitor, and only cancel/reprice under bounded rules.
        if open_orders:
            sell = open_orders[0]
            key = sell.intent.client_order_id
            if not (sell.intent.side is Side.SELL and sell.intent.reduce_only):
                self.exit_block_reason = 'EXIT_UNEXPECTED_OPEN_ORDER'
                return None
            if self._delete_already_consumed(key):
                self.exit_block_reason = 'EXIT_CANCEL_ALREADY_CONSUMED'
                return None
            if key not in self.grid.orders:
                self.exit_block_reason = None
                return None
            current_book = self._fresh_exit_book(book=book)
            target_price, _, block_reason = self._plan_limit_exit(account.inventory, current_book)
            record = self.gateway.state.orders[key]
            age_seconds = (self.gateway.clock() - record.observed_at).total_seconds()
            if (block_reason is None and (key in self.grid.exit_cancellations
                    or (age_seconds >= self.exit_reprice_seconds
                        and sell.intent.limit_price is not None and target_price < sell.intent.limit_price))):
                # A pending cancel command is also rechecked after replay. Keep
                # the working SELL unless today's replacement is admissible.
                try:
                    reference = self.gateway.fresh_mark_price(self.grid.instrument_id)
                    self.gateway.check_price_band(self.grid.instrument_id, target_price, reference,
                                                  quote_price=current_book['askPrice'])
                except SafetyBlocked as error:
                    self.exit_block_reason = _classify_exit_block(error) or 'EXIT_MARK_UNAVAILABLE'
                    return None
                if key not in self.grid.exit_cancellations:
                    self._command('exit_cancel', key)
                result = self.controller.cancel(key) if self.session is None else self.session.cancel(key)
                self._exit_awaiting_reconcile = True
                self.ready = result is not None and not (self.session and self.session.write_uncertain)
                self._sync()
                return result
            self.exit_block_reason = None
            return None

        # No open exchange order and confirmed long inventory > 0: propose one legal reduce-only SELL LIMIT.
        current_book = self._fresh_exit_book(book=book)
        price, quantity, block_reason = self._plan_limit_exit(account.inventory, current_book)
        if block_reason is not None:
            self.exit_block_reason = block_reason
            return None
        self.exit_block_reason = None
        self._command('exit_limit', dict(price=price, quantity=quantity))
        intent = self.grid.active[0].intent
        if (intent.side is not Side.SELL or intent.order_type is not OrderType.LIMIT
                or not intent.reduce_only or intent.quantity > account.inventory):
            self.withdraw_unsubmitted()
            raise SafetyBlocked('breakout exit requires reduce-only SELL LIMIT within confirmed inventory')
        try:
            if self.session is None:
                result = self.controller.submit(intent)
            else:
                self.expect(intent)
                result = self.session.submit_intent(intent, book=current_book)
        except (SafetyBlocked, RiskRejected) as error:
            self.withdraw_unsubmitted()
            reason = _classify_exit_block(error)
            if reason is not None:
                self.exit_block_reason = reason
                return None
            raise
        self.ready = result is not None and not (self.session and self.session.write_uncertain)
        if not self.ready or getattr(result, 'status', None) == OrderStatus.REJECTED:
            self._exit_awaiting_reconcile = True
        self._sync()
        return result

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
