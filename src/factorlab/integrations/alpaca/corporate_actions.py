"""Official nontrade evidence retained in the existing portfolio journal."""
import json
from datetime import date, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from factorlab.trading.storage import decode


class PreparationIncomplete(RuntimeError):
    """The market supplied no complete execution-basis preparation yet."""


def read_announcements(http, *, since, until):
    """Read inclusive date windows without accepting an ambiguous saturated day.

    Preserve official records verbatim: some non-dividend announcements do not
    carry an ex-date even when the request selects that date basis.
    """
    start, end = date.fromisoformat(since), date.fromisoformat(until)
    if start > end:
        raise ValueError('corporate action window is reversed')
    windows, records, observed = [(start, end)], {}, {}
    while windows:
        first, last = windows.pop()
        rows = http.request('GET', '/v2/corporate_actions/announcements', params={
            'ca_types': 'dividend,merger,spinoff,split', 'since': first.isoformat(),
            'until': last.isoformat(), 'date_type': 'ex_date'})
        for row in rows:
            identity = row['id']
            if identity in observed and observed[identity] != row:
                raise PreparationIncomplete('CORPORATE_ANNOUNCEMENT_CHANGED:' + identity)
            observed[identity] = row
        if len(rows) >= 1000:
            if first == last:
                raise PreparationIncomplete('SATURATED_CORPORATE_ACTION_DAY:' + first.isoformat())
            middle = first + (last - first) // 2
            windows.extend(((middle + timedelta(days=1), last), (first, middle)))
            continue
        for row in rows:
            records[row['id']] = row
    missing = observed.keys() - records.keys()
    if missing:
        raise PreparationIncomplete('CORPORATE_ANNOUNCEMENT_DISAPPEARED:' + min(missing))
    return list(records.values())


class CorporateReceiptJournal:
    """Keep source receipts independently of whether economic posting is ready."""

    def __init__(self, store, account_id):
        self.store, self.account_id = store, account_id

    def ingest(self, receipts):
        """Atomically retain exact official identities, rejecting changed receipts."""
        with self.store.transaction():
            for receipt in receipts:
                identity = receipt['id']
                if not identity or not receipt['activity_type']:
                    raise ValueError('official corporate receipt identity required')
                self.store.record_metadata(self.key(identity), dict(account=self.account_id, receipt=receipt))

    def key(self, identity):
        return 'alpaca-corporate-receipt:' + self.account_id + ':' + identity

    def read(self, identity):
        payload = self.store.event_payload(self.key(identity))
        return None if payload is None else decode(json.loads(payload))['receipt']

    def _pending(self, strategy_id, announcements, receipts, entitlement):
        claims = self.store.holdings(self.account_id, strategy_id)
        pending, blocked = {}, {}
        orders = [decode(json.loads(payload)) for payload, in self.store.db.execute(
            'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
            'WHERE p.account=?', (self.account_id,))]
        for receipt in receipts:
            symbol = receipt.get('symbol')
            if symbol not in claims:
                continue
            identity = 'alpaca-nta:' + receipt['id']
            if self.store.db.execute(
                    'SELECT 1 FROM portfolio_corporate_actions WHERE account=? AND strategy=? AND event=?',
                    (self.account_id, strategy_id, identity)).fetchone():
                continue
            kind = receipt['activity_type']
            if kind not in {'SSP', 'DIV', 'DIVNRA', 'DIVFEE'}:
                blocked[symbol] = 'OFFICIAL_CORPORATE_ALLOCATION_REQUIRED'
                continue
            if receipt.get('status') not in {None, 'executed'}:
                blocked[symbol] = 'CORPORATE_RECEIPT_NOT_EXECUTED'
                continue
            ratio, cash = D(1), D(str(receipt['net_amount']))
            effective_date = date.fromisoformat(receipt['date'][:10])
            evidence = {'receipt': receipt}
            if kind == 'SSP':
                matches = [row for row in announcements if row['ca_type'].lower() == 'split'
                           and row['initiating_symbol'] == symbol
                           and row['ex_date'] == effective_date.isoformat()]
                if len(matches) != 1:
                    blocked[symbol] = 'AMBIGUOUS_OFFICIAL_SPLIT_TERMS'
                    continue
                announcement = matches[0]
                old, new = D(str(announcement['old_rate'])), D(str(announcement['new_rate']))
                if not old.is_finite() or not new.is_finite() or old <= 0 or new <= 0:
                    blocked[symbol] = 'INVALID_OFFICIAL_SPLIT_TERMS'
                    continue
                if cash != 0:
                    blocked[symbol] = 'SPLIT_CASH_IN_LIEU_REQUIRES_BASIS'
                    continue
                ratio = new / old
                evidence['announcement'] = announcement
            elif kind in {'DIV', 'DIVNRA'} and D(str(receipt['qty'])) != entitlement[symbol]:
                blocked[symbol] = 'DIVIDEND_ENTITLEMENT_OWNERSHIP_REQUIRED'
                continue
            # TODO: consume individual official execution chronology to attribute
            # date-only entitlements when an order crosses the action boundary.
            affected = [o for o in orders if o['record'].snapshot.intent.instrument_id == symbol]
            if any(o['record'].observed_at.astimezone(ZoneInfo('America/New_York')).date()
                   >= effective_date for o in affected):
                blocked[symbol] = 'CORPORATE_ACTION_EXECUTION_CHRONOLOGY_REQUIRED'
                continue
            pending.setdefault(symbol, []).append(dict(id=identity, symbol=symbol, quantity_ratio=ratio,
                basis_ratio=D(1), cash_flow=cash, evidence=evidence))
        return pending, blocked

    def _post(self, executor, pending, blocked):
        for symbol, by_strategy in pending.items():
            if symbol in blocked:
                continue
            try:
                executor.record_account_corporate_actions(account_id=self.account_id, by_strategy=by_strategy)
            except ValueError as error:
                if str(error) not in {'corporate action does not reconcile to official shares',
                                      'corporate action requires settled original orders',
                                      'cannot mix effective-time and legacy anchored corporate actions'}:
                    raise
                blocked[symbol] = str(error)
        return blocked

    def reconcile(self, executor, *, strategy_id, announcements, receipts):
        self.ingest(receipts)
        pending, blocked = self._pending(strategy_id, announcements, receipts,
                                         self.store.holdings(self.account_id, strategy_id))
        return self._post(executor, {s: {strategy_id: actions} for s, actions in pending.items()}, blocked)

    def reconcile_account(self, executor, *, announcements, receipts):
        """Allocate one receipt across proven sleeves, then post all owners atomically.

        No post-action current-position guess is allowed: crossing chronology is
        blocked for the affected security. Last-owner residual preserves the exact
        official Decimal cash total without multiplying account cash by instances.
        """
        self.ingest(receipts)
        total = self.store.holdings(self.account_id)
        owners = sorted({owner for owner, in self.store.db.execute(
            'SELECT DISTINCT strategy FROM portfolio_claims WHERE account=?', (self.account_id,))})
        grouped, blocked = {}, {}
        for owner in owners:
            pending, reasons = self._pending(owner, announcements, receipts, total)
            blocked.update(reasons)
            for symbol, actions in pending.items():
                grouped.setdefault(symbol, {})[owner] = actions
        for receipt in receipts:
            symbol, identity = receipt.get('symbol'), 'alpaca-nta:' + receipt['id']
            pending_owners = {owner for owner, actions in grouped.get(symbol, {}).items()
                              if any(terms['id'] == identity for terms in actions)}
            posted_owners = {owner for owner, in self.store.db.execute(
                'SELECT strategy FROM portfolio_corporate_actions WHERE account=? AND event=?',
                (self.account_id, identity))}
            if pending_owners and posted_owners:
                blocked[symbol] = 'PARTIALLY_POSTED_ACCOUNT_CORPORATE_RECEIPT'
        for symbol, by_owner in grouped.items():
            holdings = {owner: self.store.holdings(self.account_id, owner)[symbol] for owner in by_owner}
            entitlement = sum(holdings.values(), D(0))
            if len(by_owner) == 1:
                continue
            if entitlement <= 0:
                blocked[symbol] = 'CORPORATE_ENTITLEMENT_OWNERSHIP_REQUIRED'
                continue
            assigned = {}
            ordered = sorted(by_owner)
            for owner in ordered:
                for terms in by_owner[owner]:
                    identity, cash = terms['id'], terms['cash_flow']
                    amount = (cash - assigned.get(identity, D(0)) if owner == ordered[-1]
                              else cash * holdings[owner] / entitlement)
                    terms['cash_flow'] = amount
                    terms['evidence'] = dict(terms['evidence'], allocation={
                        'strategy': owner, 'quantity': str(holdings[owner]), 'total_quantity': str(entitlement)})
                    assigned[identity] = assigned.get(identity, D(0)) + amount
        return self._post(executor, grouped, blocked)
