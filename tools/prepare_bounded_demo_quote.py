"""Prepare an unapproved bounded Demo quote from GET-only observations."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import uuid

from factorlab.integrations.binance_futures_trading.demo_preflight import inspect_demo
from factorlab.integrations.binance_futures_trading.preflight import demo_credential_dialog
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment

D = Decimal


def proposal(observation, client_id):
    checks = observation['checks']
    blockers = [name for name, check in checks.items()
                if name != 'api_key_trade' and check['eligible'] is not True]
    if blockers:
        return {'status': 'P7_M04_BLOCKED', 'blockers': blockers}
    value = lambda key: checks[key]['value']
    tick = value('PRICE_FILTER')['tickSize']
    step = value('LOT_SIZE')['stepSize']
    floor = lambda amount: (amount / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    ceil = lambda amount: (amount / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    cents = lambda amount: amount.quantize(D('.01'), rounding=ROUND_CEILING)
    bid, ask = value('book')['bidPrice'], value('book')['askPrice']
    mark = value('mark')['markPrice']
    band_low = mark * value('PERCENT_PRICE')['multiplierDown']
    band_high = mark * value('PERCENT_PRICE')['multiplierUp']
    minimum = max(ceil(band_low), ceil(value('PRICE_FILTER')['minPrice']), floor(bid * D('.99')))
    maximum = min(floor(band_high), floor(value('PRICE_FILTER')['maxPrice']), ceil(bid * D('1.005')))
    candidate = floor(bid * D('.995'))
    ask_ceiling = ceil(ask * D('1.01'))
    quantity = (max(value('LOT_SIZE')['minQty'], value('MIN_NOTIONAL')['notional'] / minimum)
                / step).to_integral_value(rounding=ROUND_CEILING) * step
    if not minimum <= candidate <= maximum or candidate >= bid:
        return {'status': 'P7_M04_BLOCKED', 'blockers': ['NO_LEGAL_CANDIDATE']}
    if quantity > value('LOT_SIZE')['maxQty']:
        return {'status': 'P7_M04_BLOCKED', 'blockers': ['QUANTITY_FILTER']}
    notional = cents(quantity * max(maximum, ask_ceiling, mark))
    margin = cents(notional * D('1.001'))
    loss = cents(quantity * maximum * D('.03') + 2 * notional * D('.001'))
    if margin > value('usdt')['availableBalance']:
        return {'status': 'P7_M04_BLOCKED', 'blockers': ['INSUFFICIENT_COLLATERAL']}
    expiry = observation['observed_at'] + timedelta(hours=2)
    policy = dict(version=1, environment='DEMO', account_id=value('account_id'), symbol='NVDAUSDT',
                  side='BUY', order_type='LIMIT', time_in_force='GTC', position_side='BOTH', reduce_only=False,
                  client_order_id=client_id, client_id_prefix=client_id[:-3], offset_bps=D('50'),
                  min_limit_price=minimum, max_limit_price=maximum, max_ask_price=ask_ceiling,
                  quantity=quantity, max_quantity=quantity, max_notional=notional, max_margin=margin,
                  max_loss=loss, max_orders=1, max_open_orders=1, max_seconds=600,
                  expires_at=expiry, max_quote_age_seconds=5,
                  cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
    return dict(status='PENDING_EXPLICIT_AUTHORIZATION', policy=policy,
                observed_candidate_limit=candidate, actual_notional=quantity*candidate,
                conservative_notional=quantity*max(candidate, ask),
                fee_reserve=quantity*max(candidate, ask)*D('.001'),
                total_reserve=quantity*max(candidate, ask)*D('1.001'),
                exchange_price_band=[band_low, band_high],
                budget_basis={'offset_bps': '50 fixed', 'min_limit_price': 'bid * 0.99 rounded down, within current filters/band',
                              'max_limit_price': 'bid * 1.005 rounded up, within current filters/band',
                              'max_ask_price': 'ask * 1.01 rounded up to tick',
                              'quantity': 'minimum step-aligned size meeting minimum notional at approved lower limit',
                              'max_notional': 'quantity * max(approved upper limit, approved ask ceiling), rounded up to cents',
                              'max_margin': 'max_notional * 1.001, rounded up to cents; full-notional collateral model',
                              'max_loss': 'quantity * upper limit * 0.03 + 2 * max_notional * 0.001, rounded up to cents'},
                write_authorization=False, max_loss_semantics='Admission threshold only; no guaranteed loss cap or automatic close')


def verify_proposal(observation, facts, rules):
    from contextlib import closing
    from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
    from factorlab.integrations.binance_futures_trading.bounded_quote import (
        BoundedQuotePolicy, BoundedQuoteObservation, bounded_quote, canonical_json)
    from factorlab.integrations.binance_futures_trading.plan_review import review_opening_plan
    from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
    from factorlab.trading.control import TradingController, RiskLimits
    from factorlab.trading.paper import PaperBroker, Instrument, Quote
    from factorlab.trading.storage import PaperStore

    identity = observation['checks']['order_identity']['value']['clientOrderId']
    candidate = proposal(observation, identity)
    if candidate['status'] == 'P7_M04_BLOCKED':
        return candidate
    p = candidate['policy']
    quote_policy = BoundedQuotePolicy(account_id=p['account_id'], client_order_id=identity,
        quantity=p['quantity'], offset_bps=p['offset_bps'], min_price=p['min_limit_price'],
        max_price=p['max_limit_price'], max_ask=p['max_ask_price'], max_notional=p['max_notional'],
        max_fee=(p['max_notional']*D('.001')).quantize(D('.01'), rounding=ROUND_CEILING),
        max_funds=p['max_margin'], fee_rate=D('.001'), expires_at=p['expires_at'], max_quote_age_seconds=5)
    plan = DemoExecutionPlan(**{key: p[key] for key in (
        'account_id', 'max_quantity', 'max_notional', 'max_orders', 'max_open_orders',
        'max_seconds', 'max_loss', 'max_margin', 'expires_at', 'client_id_prefix', 'cleanup', 'recovery')},
        order_types=frozenset({'LIMIT'}), quote_policy=quote_policy)
    now = observation['observed_at']
    mark = observation['checks']['mark']['value']['markPrice']
    ask = observation['checks']['book']['value']['askPrice']
    intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
                         p['quantity'], candidate['observed_candidate_limit'], identity)
    review = review_opening_plan(plan, facts=facts, rules=rules, mark_price=mark,
                                quote_price=ask, limit_price=intent.limit_price, now=now)
    official = rules.validate(intent, mark_price=mark, leverage=observation['checks']['leverage']['value'],
                              max_leverage=observation['checks']['leverage']['value'],
                              available_margin=facts.usdt()['availableBalance'], current_notional=D('0'), now=now)
    broker = PaperBroker(Market.BINANCE_USDM, p['account_id'], (Instrument('NVDAUSDT', 'USDT'),),
                         {'USDT': facts.usdt()['availableBalance']}, fee_rate=D('.001'))
    with closing(PaperStore(':memory:')) as store:
        controller = TradingController(broker, store, clock=lambda: now,
            limits=RiskLimits(p['max_notional'], p['max_notional'], p['max_notional'], 1, 5))
        controller.quote(Quote('NVDAUSDT', ask, now))
        observed_risk = controller.assess(intent)
        controller.quote(Quote('NVDAUSDT', max(p['max_ask_price'], p['max_limit_price'], mark), now))
        boundary_risk = controller.assess(intent)
    candidate['admission_reviews'] = dict(opening_plan=review, actual_fixed_quantity_rules=official.reason,
                                           controller_observed=observed_risk.reason,
                                           controller_upper_boundary=boundary_risk.reason)
    if any(review.values()) or not all(x.approved for x in (official, observed_risk, boundary_risk)):
        candidate['status'] = 'P7_M04_BLOCKED'
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-dialog', action='store_true')
    parser.add_argument('--private-output', required=True, type=Path)
    parser.add_argument('--public-output', required=True, type=Path)
    args = parser.parse_args()
    credentials = demo_credential_dialog() if args.credential_dialog else Credentials.from_env(Environment.DEMO)
    if credentials is None:
        print('CANCELLED: no requests sent')
        return
    client_id = 'm04-' + uuid.uuid4().hex[:16] + '-01'
    observation = inspect_demo(credentials, client_order_id=client_id, prepare_policy=verify_proposal)
    credentials = None
    candidate = observation.pop('bounded_preparation')
    receipt = dict(observation=observation, candidate=candidate,
                   actual_network_requests={'GET': len(observation['endpoints']), 'POST': 0, 'DELETE': 0,
                                            'account_configuration_writes': 0})
    args.private_output.parent.mkdir(parents=True, exist_ok=True)
    with args.private_output.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, indent=2, default=str)
    account = observation['checks']['account_id']['value']
    if isinstance(account, str) and account.startswith('DEMO:FactorLab-Demo:'):
        public = json.dumps(receipt, indent=2, default=str).replace(account, 'DEMO:FactorLab-Demo:sha256:...' + account[-8:])
    else:
        public = json.dumps(receipt, indent=2, default=str)
    args.public_output.parent.mkdir(parents=True, exist_ok=True)
    with args.public_output.open('x', encoding='utf-8') as stream:
        stream.write(public + '\n')
    print(json.dumps({'status': candidate['status'], 'requests': receipt['actual_network_requests'],
                      'public_output': str(args.public_output)}, indent=2))


if __name__ == '__main__':
    main()
