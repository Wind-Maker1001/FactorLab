"""User-data event parser only; no listen-key creation or socket connection."""
from decimal import Decimal
import hashlib
import json

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal
from factorlab.trading import OrderEvent

from .account import instant
from .adapter import STATUS

ZERO = Decimal('0')


class UserDataEvents:
    """Persisted deduplication; order gaps/late reports close reconciliation gate.

    Binance user streams have no replayable sequence cursor. Reconnect must call
    adapter.reconcile(); silence is never interpreted as proof of completeness.
    ACCOUNT_UPDATE is a partial delta, not an entire AccountSnapshot.
    """
    def __init__(self, adapter):
        self.adapter = adapter

    def disconnected(self):
        self.adapter.disconnect()

    def parse(self, message):
        try:
            return self._parse(message)
        except (KeyError, TypeError, ValueError, DataQualityError):
            self.adapter.disconnect()
            raise DataQualityError('user event incomplete, out of order or conflicting; REST reconciliation required') from None

    def _parse(self, message):
        kind = message['e']
        if kind == 'listenKeyExpired':
            self.adapter.disconnect()
            return None
        if kind == 'ACCOUNT_UPDATE':
            # Positions/balances lists only contain changed entries. A complete
            # authenticated re-read supplies missing margin and risk fields.
            instant(message['E'])
            instant(message['T'])
            reason = message['a']['m']
            if not isinstance(message['a']['B'], list) or not isinstance(message['a']['P'], list):
                raise DataQualityError('invalid account update')
            self.adapter.disconnect()
            return {'reason': reason, 'balances': tuple(message['a']['B']),
                    'positions': tuple(message['a']['P']), 'reconcile_required': True}
        if kind != 'ORDER_TRADE_UPDATE':
            raise DataQualityError('not an authenticated account/order event')
        row = message['o']
        key = 'ws:' + hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        duplicate = self.adapter.db.execute('SELECT payload FROM futures_events WHERE id=?', (key,)).fetchone()
        if duplicate is not None:
            from factorlab.trading.storage import decode
            return decode(json.loads(duplicate[0]))
        record, previous_quote = self.adapter._load(row['c'])
        intent = record.snapshot.intent
        if (row['s'] != intent.instrument_id or row['S'] != intent.side.value or row['ps'] != 'BOTH'
                or row['o'] != intent.order_type.value or _decimal(row['q']) != intent.quantity):
            raise DataQualityError('user event identity mismatch')
        filled, last_qty, last_price = _decimal(row['z']), _decimal(row['l']), _decimal(row['L'])
        delta = filled - record.snapshot.cumulative_filled_quantity
        if row['x'] == 'TRADE':
            if delta != last_qty or delta <= 0 or row.get('N') != 'USDT':
                raise DataQualityError('missing trade/fee or non-USDT fee; reconcile')
            fee = record.snapshot.cumulative_fee + _decimal(row['n'])
            quote = previous_quote + delta * last_price
        else:
            if delta != 0:
                raise DataQualityError('missed execution before status report')
            fee, quote = record.snapshot.cumulative_fee, previous_quote
        event = OrderEvent(row['c'], str(row['i']), STATUS[row['X']], filled,
                           last_price if delta else None, instant(row['T']), 'binance-user-data', key, fee)
        return self.adapter._apply(event, quote)
