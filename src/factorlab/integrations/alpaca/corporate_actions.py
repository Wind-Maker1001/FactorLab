"""Official nontrade evidence retained in the existing portfolio journal."""
import json
from datetime import date
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from factorlab.trading.storage import decode


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
                self.store.record_metadata(self.key(identity),
                    dict(account=self.account_id, receipt=receipt))

    def key(self, identity):
        return 'alpaca-corporate-receipt:' + self.account_id + ':' + identity

    def read(self, identity):
        payload = self.store.event_payload(self.key(identity))
        return None if payload is None else decode(json.loads(payload))['receipt']

    def reconcile(self, executor, *, strategy_id, announcements, receipts):
        """Post settled, unambiguous split facts; retain unresolved rights as evidence.

        Date-only announcements establish a boundary only if all affected order
        confirmations precede that local date. A crossing order needs individual
        execution chronology and is not approximated by its update timestamp.
        """
        self.ingest(receipts)
        claims = {symbol: D(quantity) for symbol, quantity in self.store.db.execute(
            'SELECT symbol,quantity FROM portfolio_claims WHERE account=? AND strategy=?',
            (self.account_id, strategy_id))}
        pending, blocked = {}, {}
        for receipt in receipts:
            symbol = receipt.get('symbol')
            if symbol not in claims:
                continue
            identity = 'alpaca-nta:' + receipt['id']
            if self.store.db.execute(
                    'SELECT 1 FROM portfolio_corporate_actions WHERE account=? AND event=?',
                    (self.account_id, identity)).fetchone():
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
                matches = [row for row in announcements
                           if row['ca_type'].lower() == 'split'
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
            elif kind in {'DIV', 'DIVNRA'} and D(str(receipt['qty'])) != claims[symbol]:
                blocked[symbol] = 'DIVIDEND_ENTITLEMENT_OWNERSHIP_REQUIRED'
                continue
            # Without exact fill chronology, refuse dates that overlap any
            # affected order, including a settled post-action execution.
            orders = [decode(json.loads(payload)) for payload, in self.store.db.execute(
                'SELECT o.payload FROM portfolio_orders o JOIN portfolio_plans p ON p.id=o.plan '
                'WHERE p.account=? AND p.strategy=?', (self.account_id, strategy_id))]
            affected = [order for order in orders
                        if order['record'].snapshot.intent.instrument_id == symbol]
            if any(order['record'].observed_at.astimezone(ZoneInfo('America/New_York')).date()
                   >= effective_date for order in affected):
                blocked[symbol] = 'CORPORATE_ACTION_EXECUTION_CHRONOLOGY_REQUIRED'
                continue
            pending.setdefault(symbol, []).append(dict(id=identity, symbol=symbol,
                quantity_ratio=ratio, basis_ratio=D(1), cash_flow=cash, evidence=evidence))
        for symbol, actions in pending.items():
            if symbol in blocked:
                continue
            # Each symbol is independent; unrelated unresolved rights do not
            # prevent another symbol's fully evidenced batch from posting.
            try:
                executor.record_corporate_actions(account_id=self.account_id,
                    strategy_id=strategy_id, actions=actions)
            except ValueError as error:
                if str(error) not in {'corporate action does not reconcile to official shares',
                                      'corporate action requires settled original orders'}:
                    raise
                blocked[symbol] = str(error)
        return blocked
