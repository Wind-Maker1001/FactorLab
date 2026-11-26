"""Official Paper execution and restart recovery; no local fill authority."""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from factorlab.trading.contracts import (
    AccountSnapshot, AssetBalance, Market, OrderEvent, OrderIntent, OrderSnapshot,
    OrderStatus as S, OrderType, Position, Side, TERMINAL,
)
from factorlab.trading.control import RiskLimits, RiskRejected
from factorlab.trading.contracts import RiskDecision

from .http import AlpacaError, OutcomeUnknown
from .models import AlpacaAccount, instant, order_parameters
from .storage import AlpacaStore

D = Decimal
Z = D("0")
CORPORATE_ACTIVITY_TYPES = frozenset({'DIV', 'DIVNRA', 'DIVFEE', 'SSP', 'SSO', 'REORG'})
STATUS = {
    "new": S.SUBMITTED, "accepted": S.SUBMITTED, "pending_new": S.SUBMITTED,
    "accepted_for_bidding": S.SUBMITTED, "pending_cancel": S.SUBMITTED,
    "pending_replace": S.SUBMITTED, "stopped": S.SUBMITTED, "suspended": S.SUBMITTED,
    "calculated": S.SUBMITTED, "partially_filled": S.PARTIALLY_FILLED,
    "filled": S.FILLED, "canceled": S.CANCELED, "expired": S.CANCELED,
    "rejected": S.REJECTED, "replaced": S.CANCELED, "done_for_day": S.SUBMITTED,
}


class OrderNotFound(AlpacaError):
    pass


def order_status(row):
    status = STATUS[row["status"]]
    if status == S.SUBMITTED and D(row["filled_qty"]) > 0:
        return S.PARTIALLY_FILLED
    return status


def intent_from_order(row):
    if row["type"] not in {"market", "limit"} or row["qty"] is None:
        raise ValueError(
            f"Alpaca order cannot be projected: id={row['id']}, type={row['type']}, qty={row['qty']}; "
            "only quantity-based market/limit history is supported; reconciliation incomplete")
    return OrderIntent(Market.ALPACA_US_EQUITY, row["symbol"], Side(row["side"].upper()),
                       OrderType(row["type"].upper()), D(row["qty"]),
                       D(row["limit_price"]) if row["limit_price"] else None,
                       row["client_order_id"], row["time_in_force"].upper())


class AlpacaAdapter:
    def __init__(self, http, store, *, limits=RiskLimits()):
        self.http = http
        self.store = store if isinstance(store, AlpacaStore) else AlpacaStore(store)
        self.limits = limits
        self.reconciled = False

    def read_account_details(self):
        account = AlpacaAccount.from_payload(self.http.request("GET", "/v2/account"))
        with self.store.transaction():
            identity = self.store.get("account_id")
            if identity is not None and identity != account.account_id:
                raise ValueError("persisted Alpaca account identity mismatch")
            self.store.put("account_id", account.account_id)
        return account

    def read_asset(self, symbol):
        return self.http.request("GET", "/v2/assets/" + quote(symbol, safe=""))

    def _lookup(self, client_id):
        try:
            return self.http.request("GET", "/v2/orders:by_client_order_id",
                                     params={"client_order_id": client_id})
        except AlpacaError as error:
            if error.status == 404:
                raise OrderNotFound(404) from None
            raise

    def _event(self, row, previous=None, *, advance=True):
        quantity = D(row["filled_qty"])
        cursor_key = "priced-event:" + row["client_order_id"]
        cursor = self.store.get(cursor_key) or {"quantity": "0", "amount": "0"}
        old_qty, old_amount = D(cursor["quantity"]), D(cursor["amount"])
        price, status = None, order_status(row)
        if quantity > old_qty:
            executions = [activity for activity in self.store.activities() if activity["order_id"] == row["id"]]
            executed = sum((D(activity["qty"]) for activity in executions), Z)
            if executed != quantity:
                self.reconciled = False
                status, quantity = S.UNKNOWN, old_qty
            else:
                amount = sum((D(activity["qty"]) * D(activity["price"]) for activity in executions), Z)
                price = (amount - old_amount) / (quantity - old_qty)
                if advance:
                    self.store.put(cursor_key, {"quantity": str(quantity), "amount": str(amount)})
        return OrderEvent(row["client_order_id"], row["id"], status, quantity, price,
                          instant(row["updated_at"] or row["created_at"]), "alpaca-paper-rest",
                          f"{row['id']}:{row['updated_at']}:{status.value}:{quantity}:{price}")

    def _apply(self, row, *, activities=None, emit=True):
        if row.get("asset_class", "us_equity") != "us_equity":
            self.reconciled = False
            raise ValueError(f"unsupported Alpaca order asset_class: id={row['id']}, asset_class={row['asset_class']}")
        key = row["client_order_id"]
        cursor = self.store.get("priced-event:" + key) or {"quantity": "0"}
        if activities is None:
            activities = self.read_activities() if D(row["filled_qty"]) > D(cursor["quantity"]) else ()
        with self.store.transaction():
            for activity in activities:
                self.store.activity(activity)
            old = self.store.order(key)
            if old is None:
                intent = intent_from_order(row)
                self.store.admit(intent, D(row["limit_price"] or "0"))
                previous = None
            else:
                intent, previous, _, _ = old
                if intent != intent_from_order(row):
                    raise ValueError("official order differs from persisted intent")
            if previous is not None:
                if previous["id"] != row["id"]:
                    raise ValueError("exchange order identity changed")
                if (instant(row["updated_at"] or row["created_at"]) <
                        instant(previous["updated_at"] or previous["created_at"])):
                    return self._event(previous, previous, advance=emit)
                if D(row["filled_qty"]) < D(previous["filled_qty"]):
                    raise ValueError("official cumulative fill regression")
                if order_status(previous) in TERMINAL and order_status(row) not in TERMINAL:
                    if D(row["filled_qty"]) == D(previous["filled_qty"]):
                        return self._event(previous, previous, advance=emit)
                    raise ValueError("terminal order returned to an open status")
                if order_status(previous) == S.FILLED and order_status(row) != S.FILLED:
                    return self._event(previous, previous, advance=emit)
            filled = D(row["filled_qty"])
            if not filled.is_finite() or filled < 0 or filled > intent.quantity:
                raise ValueError("invalid official cumulative fill")
            if (order_status(row) == S.FILLED) != (filled == intent.quantity):
                raise ValueError("official FILLED status and quantity disagree")
            if filled and (row["filled_avg_price"] is None or D(row["filled_avg_price"]) <= 0):
                raise ValueError("official fill requires a positive average price")
            event = self._event(row, previous, advance=emit)
            self.store.save_order(key, row)
            if previous is None or D(previous["filled_qty"]) != filled:
                self.reconciled = False
            if event.status == S.PARTIALLY_FILLED:
                self.store.put("partial:" + key, {field: row[field] for field in
                                                 ("id", "client_order_id", "filled_qty", "filled_avg_price", "updated_at")})
            return event

    def query_order(self, client_order_id):
        rejection = self.store.get("rejection:" + client_order_id)
        if rejection is not None and self.store.order(client_order_id)[1] is None:
            return OrderEvent(client_order_id, None, S.REJECTED, Z, None,
                              instant(rejection["time"]), "alpaca-paper-http",
                              "http-rejection:" + client_order_id)
        return self._apply(self._lookup(client_order_id))

    def read_order_details(self, client_order_id):
        """Authoritative cumulative facts, independent of any consumer's event cursor.

        filled_avg_price is the broker's rounded cumulative average. Consumers
        needing execution-exact cash flows use read_order_executions instead.
        """
        self._apply(self._lookup(client_order_id), emit=False)
        return self.stored_order_details(client_order_id)

    def stored_order_details(self, client_order_id):
        row = self.store.order(client_order_id)[1]
        return {"client_order_id": row["client_order_id"], "exchange_order_id": row["id"],
                "status": order_status(row), "filled_quantity": D(row["filled_qty"]),
                "filled_avg_price": D(row["filled_avg_price"]) if row["filled_avg_price"] else None,
                "cumulative_filled_notional": D(row["filled_qty"]) * D(row["filled_avg_price"] or "0"),
                "observed_at": instant(row["updated_at"] or row["created_at"])}

    def read_order_executions(self, client_order_id):
        row = self._lookup(client_order_id)
        activities = self.read_activities()
        self._apply(row, activities=activities, emit=False)
        return tuple(activity for activity in self.store.activities() if activity["order_id"] == row["id"])

    def _reject(self, reason):
        raise RiskRejected(RiskDecision(False, reason))

    def _positions(self):
        rows = self.http.request("GET", "/v2/positions")
        for row in rows:
            if row.get("asset_class", "us_equity") != "us_equity":
                self.reconciled = False
                raise ValueError(f"unsupported Alpaca position asset_class: symbol={row['symbol']}, asset_class={row['asset_class']}")
        return rows

    def read_account(self):
        facts = self.read_account_details()
        rows = self._positions()
        orders, reserved = [], Z
        for _, (intent, row, uncertain, price) in self.store.orders():
            if self.store.get("rejection:" + intent.client_order_id) is not None and row is None:
                continue
            if row is not None and order_status(row) in TERMINAL and not uncertain:
                continue
            quantity = D(row["filled_qty"]) if row else Z
            status = S.UNKNOWN if uncertain else order_status(row)
            orders.append(OrderSnapshot(intent, status, quantity, Z, row["id"] if row else None, uncertain))
            if intent.side == Side.BUY:
                reserved += (intent.quantity - quantity) * D(price)
        return AccountSnapshot(Market.ALPACA_US_EQUITY, facts.account_id,
                               (AssetBalance("USD", facts.cash, reserved),),
                               tuple(Position(p["symbol"], D(p["qty"]), D(p["avg_entry_price"]),
                                              D(p["current_price"]), "USD") for p in rows),
                               tuple(orders), facts.observed_at, self.reconciled)

    def _admission(self, intent, price, account, facts):
        if facts.status != "ACTIVE" or facts.account_blocked or facts.trading_blocked:
            self._reject("ACCOUNT_BLOCKED")
        unresolved = any(uncertain and self.store.get("rejection:" + key) is None
                         for key, (_, _, uncertain, _) in self.store.orders())
        if not self.reconciled or unresolved or any(o.reconcile_required for o in account.open_orders):
            self._reject("RECONCILE_REQUIRED")
        if any(p.quantity < 0 for p in account.positions):
            self._reject("LONG_ONLY_ACCOUNT_REQUIRED")
        if intent.side == Side.SELL:
            owned = sum((p.quantity for p in account.positions if p.instrument_id == intent.instrument_id), Z)
            reserved = sum((o.intent.quantity - o.cumulative_filled_quantity for o in account.open_orders
                            if o.intent.instrument_id == intent.instrument_id and o.intent.side == Side.SELL), Z)
            if intent.quantity > owned - reserved:
                self._reject("INSUFFICIENT_INVENTORY")
            return
        amount = intent.quantity * price
        reserved_cash = Z
        for key, (other, row, uncertain, reservation) in self.store.orders():
            if row is None and self.store.get("rejection:" + key) is not None:
                continue
            if uncertain or row is None or order_status(row) not in TERMINAL:
                if other.side == Side.BUY:
                    if D(reservation) <= 0:
                        self._reject("UNPRICED_EXTERNAL_ORDER")
                    reserved_cash += (other.quantity - (D(row["filled_qty"]) if row else Z)) * D(reservation)
        if amount > min(facts.cash - reserved_cash, facts.equity):
            self._reject("INSUFFICIENT_BALANCE")
        if amount > self.limits.max_order_notional:
            self._reject("ORDER_NOTIONAL_LIMIT")
        exposures = {p.instrument_id: abs(p.quantity) * p.mark_price for p in account.positions}
        for key, (other, row, _, reservation) in self.store.orders():
            if row is None and self.store.get("rejection:" + key) is not None:
                continue
            if other.side == Side.BUY and (row is None or order_status(row) not in TERMINAL):
                pending = other.quantity - (D(row["filled_qty"]) if row else Z)
                exposures[other.instrument_id] = exposures.get(other.instrument_id, Z) + pending * D(reservation)
        if exposures.get(intent.instrument_id, Z) + amount > self.limits.max_instrument_exposure:
            self._reject("INSTRUMENT_EXPOSURE_LIMIT")
        if sum(exposures.values(), Z) + amount > min(self.limits.account_budget, facts.equity):
            self._reject("ACCOUNT_BUDGET_LIMIT")

    def submit_order(self, intent, *, reservation_price=None):
        old = self.store.order(intent.client_order_id)
        if old:
            if old[0] != intent:
                raise ValueError("client_order_id reused with different intent")
            return self.query_order(intent.client_order_id)
        params = order_parameters(intent, self.read_asset(intent.instrument_id))
        price = intent.limit_price if intent.limit_price is not None else reservation_price
        if price is None or not isinstance(price, D) or not price.is_finite() or price <= 0:
            raise ValueError("market orders require an explicit positive reservation_price")
        # REST refresh before admission includes orders placed outside this process.
        self.recover()
        account, facts = self.read_account(), self.read_account_details()
        with self.store.transaction():
            self._admission(intent, price, account, facts)
            now = datetime.now(timezone.utc).timestamp()
            recent = [t for t in self.store.get("submissions") or [] if now - t < 60]
            if len(recent) >= self.limits.orders_per_minute:
                self._reject("ORDER_RATE_LIMIT")
            recent.append(now)
            self.store.put("submissions", recent)
            self.store.admit(intent, price)
        self.reconciled = False
        try:
            row = self.http.request("POST", "/v2/orders", body=params)
        except OutcomeUnknown:
            self.reconciled = False
            raise
        except AlpacaError as error:
            message = (error.message or "").lower()
            duplicate_identity = "client_order_id" in message and any(
                marker in message for marker in ("unique", "duplicate", "already"))
            if error.status in {408, 409} or (
                    error.status == 422 and (not message or duplicate_identity)):
                self.reconciled = False
                raise OutcomeUnknown("query persisted client_order_id before any replacement") from None
            with self.store.transaction():
                self.store.put("rejection:" + intent.client_order_id,
                               {"status": error.status, "time": datetime.now(timezone.utc).isoformat()})
            self.reconciled = False
            raise
        return self._apply(row)

    def cancel_order(self, client_order_id):
        row = self._lookup(client_order_id)
        self._apply(row, emit=False)
        if order_status(row) not in TERMINAL:
            with self.store.transaction():
                self.store.uncertain(client_order_id)
            self.reconciled = False
            try:
                self.http.request("DELETE", "/v2/orders/" + quote(row["id"], safe=""))
            except AlpacaError as error:
                if error.status not in {404, 422}:
                    raise
            return self.query_order(client_order_id)
        return self._apply(row, activities=())

    def read_activities(self, *, after=None):
        filters = {} if after is None else {'after': after}
        return self._activity_pages('/v2/account/activities/FILL', filters)

    def read_account_activities(self, *, after):
        return self._activity_pages('/v2/account/activities', {'after': after})

    def read_corporate_activities(self, *, after):
        """Official nontrade receipts; dividend qty is eligibility, not new shares."""
        # Paper rejects SSP as a query filter despite documenting it as a receipt type.
        rows = self.read_account_activities(after=after)
        return tuple(row for row in rows if row['activity_type'] in CORPORATE_ACTIVITY_TYPES)

    def _activity_pages(self, path, filters):
        rows, token = [], None
        for _ in range(10000):
            params = {"direction": "asc", "page_size": 100, **filters}
            if token:
                params["page_token"] = token
            page = self.http.request("GET", path, params=params)
            rows.extend(page)
            if len(page) < 100:
                return tuple(rows)
            next_token = page[-1]["id"]
            if next_token == token:
                raise ValueError("activity pagination did not advance")
            token = next_token
        raise ValueError("activity pagination budget exceeded")

    def _all_orders(self, *, after=None, status='all'):
        rows, until = [], None
        for _ in range(10000):
            params = {"status": status, "limit": 500, "direction": "desc", "nested": "false"}
            if after is not None:
                params['after'] = after
            if until:
                params["until"] = until
            page = self.http.request("GET", "/v2/orders", params=params)
            rows.extend(page)
            if len(page) < 500:
                return rows
            # Overlap the last timestamp group: `until` is exclusive, and a
            # page may cut through several orders sharing its oldest timestamp.
            timestamps = list(dict.fromkeys(row["submitted_at"] for row in page))
            if len(timestamps) < 2:
                # FIXME: timestamp-only history cannot prove coverage of a saturated page.
                raise ValueError(
                    f"Alpaca history cursor saturated: 500 orders at submitted_at={timestamps[0]}; "
                    "reconciliation incomplete, execution blocked")
            next_until = timestamps[-2]
            if next_until == until:
                raise ValueError("order pagination boundary ambiguous; reconciliation required")
            until = next_until
        raise ValueError("order pagination budget exceeded")

    def recover(self, *, after=None):
        """Recover the account, optionally using a restored ledger's history floor."""
        self.reconciled = False
        self.read_account_details()
        filters = {} if after is None else {'after': after}
        activities = self.read_activities(**filters)
        with self.store.transaction():
            for activity in activities:
                self.store.activity(activity)
        rows = self._all_orders(**filters)
        if after is not None:
            # An order submitted before the floor can remain open for days.
            rows.extend(self._all_orders(status='open'))
        for row in rows:
            self._apply(row, activities=(), emit=False)
        for key, (_, row, uncertain, _) in self.store.orders():
            if row is None and self.store.get("rejection:" + key) is not None:
                continue
            if row is None or uncertain or order_status(row) not in TERMINAL:
                try:
                    self._apply(self._lookup(key), activities=(), emit=False)
                except OrderNotFound:
                    # Absence is not proof a timed-out POST cannot still arrive.
                    pass
        activities = self.read_activities(**filters)
        with self.store.transaction():
            for row in activities:
                self.store.activity(row)
        if after is not None:
            # A fill of an older order can arrive inside the activity window even
            # if the order is already terminal by the time we list open orders.
            known = {row['id'] for _, (_, row, _, _) in self.store.orders() if row is not None}
            for order_id in sorted({row['order_id'] for row in activities} - known):
                path = '/v2/orders/' + quote(order_id, safe='')
                row = self.http.request('GET', path)
                if row['id'] != order_id:
                    raise ValueError('Alpaca requested exchange order identity mismatch')
                self._apply(row, activities=(), emit=False)
        totals = {}
        for row in self.store.activities():
            key = row["order_id"]
            qty, amount = totals.get(key, (Z, Z))
            totals[key] = qty + D(row["qty"]), amount + D(row["qty"]) * D(row["price"])
        complete = True
        for key, (_, row, uncertain, _) in self.store.orders():
            if row is None and self.store.get("rejection:" + key) is not None:
                continue
            if uncertain or row is None:
                complete = False
            else:
                qty, amount = totals.get(row["id"], (Z, Z))
                if D(row["filled_qty"]) != qty:
                    complete = False
                # Alpaca's cumulative average may be rounded; activities carry execution prices.
                if abs(amount - qty * D(row["filled_avg_price"] or "0")) > qty * D("0.0001"):
                    complete = False
        self.reconciled = complete
        return self.read_account()

    def on_trade_update(self, message):
        """Push messages trigger authoritative queries; duplicates never book a fill."""
        data = message["data"]
        event = self.query_order(data["order"]["client_order_id"])
        self.recover()
        return event

    def ledger_summary(self):
        """Execution cash flows and positions, counted once per official activity ID.

        These are fill deltas, not account cash: deposits, fees and other account
        activities are reconciled separately against the official account snapshot.
        """
        cash_flow, positions = Z, {}
        for row in self.store.activities():
            sign = D("1") if row["side"] == "buy" else D("-1")
            quantity, price = D(row["qty"]), D(row["price"])
            positions[row["symbol"]] = positions.get(row["symbol"], Z) + sign * quantity
            cash_flow -= sign * quantity * price
        return {"cash_flow": cash_flow, "positions": positions,
                "activity_count": len(self.store.activities())}

    def close(self):
        self.store.close()
