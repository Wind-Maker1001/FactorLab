"""Fixed portfolio-domain recovery dispatch, independent of runtime inference."""
import json

from .contracts import TERMINAL
from .storage import decode
from .capital_management import reduce_capital_event
from .portfolio_risk import reduce_portfolio_risk_event
from .portfolio_order_events import reduce_order_observation


def reduce_portfolio_event(db, event):
    """Reduce admitted original state in the caller's portfolio transaction."""
    if event['kind'] == 'PORTFOLIO_PLAN_ADMITTED':
        plan = decode(json.loads(event['plan']))
        request = plan['request']
        for effect in event['effects']:
            if effect['kind'] not in ('CAPITAL_SYMBOL_CLAIMED',
                                      'PORTFOLIO_RISK_MULTIPLIERS_RECORDED',
                                      'PORTFOLIO_RISK_NAV_RECORDED'):
                raise ValueError('unsupported original plan effect')
            reduce_portfolio_event(db, effect)
        for claim in event['claims']:
            db.execute('INSERT OR IGNORE INTO portfolio_claims VALUES (?,?,?,?,?,?)', claim)
        db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                   (plan['id'], request['account'], request['strategy'], request['session'], event['plan']))
        for payload in event['orders']:
            order = decode(json.loads(payload))
            snapshot = order['record'].snapshot
            active = snapshot.status not in TERMINAL or order['uncertain']
            db.execute('INSERT INTO portfolio_orders VALUES (?,?,?,?)',
                       (snapshot.intent.client_order_id, plan['id'], payload, int(active)))
        for confirmation in event['confirmations']:
            db.execute('INSERT INTO portfolio_confirmations VALUES (?,?,?)', confirmation)
        return
    if event['kind'].startswith('CAPITAL_'):
        return reduce_capital_event(db, event)
    if event['kind'] in ('PORTFOLIO_ORDER_OBSERVED', 'PORTFOLIO_ORDER_LOOKUP_RECORDED',
                         'PORTFOLIO_ORDER_ATTEMPT_RECORDED'):
        return reduce_order_observation(db, event)
    return reduce_portfolio_risk_event(db, event)
