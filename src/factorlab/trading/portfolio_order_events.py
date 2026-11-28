"""Original order observations and their economic ownership postings."""
from .contracts import TERMINAL
from .storage import decode
import json


def reduce_order_observation(db, event):
    """Apply a prepared order observation without querying a venue."""
    if event['kind'] == 'PORTFOLIO_ORDER_LOOKUP_RECORDED':
        db.execute('UPDATE portfolio_plans SET payload=? WHERE id=?',
                   (event['plan'], event['plan_id']))
    elif event['kind'] not in ('PORTFOLIO_ORDER_OBSERVED', 'PORTFOLIO_ORDER_ATTEMPT_RECORDED'):
        raise ValueError('unsupported order observation event')
    order = decode(json.loads(event['order']))
    snapshot = order['record'].snapshot
    active = snapshot.status not in TERMINAL or order['uncertain'] or order.get('cancel_pending', False)
    db.execute('UPDATE portfolio_orders SET payload=?,active=? WHERE id=?',
               (event['order'], int(active), snapshot.intent.client_order_id))
    claim = event['claim']
    if claim is not None:
        db.execute('UPDATE portfolio_claims SET quantity=? WHERE account=? AND symbol=? AND strategy=?',
                   (claim['quantity'], event['account_id'], claim['symbol'], event['strategy_id']))
    if event['confirmation'] is not None:
        db.execute('INSERT INTO portfolio_confirmations VALUES (?,?,?)',
                   (event['account_id'], event['confirmation_id'], event['confirmation']))
