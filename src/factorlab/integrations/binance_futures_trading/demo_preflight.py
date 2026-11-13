"""Observe the official Demo account and review a plan using only GET requests."""
import argparse
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
import json

from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import _decimal
from factorlab.trading import OrderType, SAFETY_LABELS
from factorlab.trading.storage import PaperStore

from .account import instant
from .adapter import FuturesAdapter
from .execution import observe_demo_rules, read_demo_symbol
from .plan_review import minimum_opening_order, review_opening_plan
from .preflight import demo_credential_dialog
from .protocol import Credentials, Environment, ExchangeError, FuturesClient, ReadOnlyNetworkTransport
from .quantitative import QuantitativeObservation
from .rules import SUPPORTED_NETWORK_FILTERS, symbol_row


DEMO = Environment.DEMO
SYMBOL = 'NVDAUSDT'
UNKNOWN = 'UNKNOWN'
ACCOUNT = '/fapi/v3/account'
EXCHANGE = '/fapi/v1/exchangeInfo'
BRACKETS = '/fapi/v1/leverageBracket'
CONFIG = '/fapi/v1/symbolConfig'
STATUS = '/fapi/v1/apiTradingStatus'
POSITION_RISK = '/fapi/v3/positionRisk'
OPEN_ORDERS = '/fapi/v1/openOrders'
MODE = '/fapi/v1/positionSide/dual'
MARK = '/fapi/v1/premiumIndex'
BOOK = '/fapi/v1/ticker/bookTicker'
FILTER_FIELDS = {
    'LOT_SIZE': ('minQty', 'maxQty', 'stepSize'),
    'MARKET_LOT_SIZE': ('minQty', 'maxQty', 'stepSize'),
    'MIN_NOTIONAL': ('notional',),
    'PRICE_FILTER': ('minPrice', 'maxPrice', 'tickSize'),
    'PERCENT_PRICE': ('multiplierDown', 'multiplierUp'),
}


@dataclass(frozen=True, kw_only=True)
class _ReadReceipt(ReadOnlyNetworkTransport):
    endpoints: list = field(default_factory=list, init=False, repr=False)

    def __call__(self, request, timeout):
        row = {'method': request.method, 'path': request.path, 'signed': bool(request.headers)}
        self.endpoints.append(row)
        response = super().__call__(request, timeout)
        row['http_status'] = response.status
        return response


def _observe(operation):
    try:
        return operation(), None
    except ExchangeError as error:
        return None, error.category
    except (DataQualityError, ValueError, TypeError, KeyError, DecimalException, OverflowError, OSError):
        return None, UNKNOWN


def _numbers(row, fields, *, nonnegative=False):
    return {key: _decimal(str(row[key]), nonnegative=nonnegative) for key in fields}


def _positions(facts):
    def nonzero(rows):
        amounts = {}
        for row in rows:
            symbol, side = row['symbol'], row['positionSide']
            identity = symbol, side
            if not isinstance(symbol, str) or not symbol or side not in {'BOTH', 'LONG', 'SHORT'} or identity in amounts:
                raise DataQualityError('unusable position identity')
            amounts[identity] = _decimal(str(row['positionAmt']))
        return {key: value for key, value in amounts.items() if value}

    account, risk = nonzero(facts.positions), nonzero(facts.risk)
    if not facts.source_complete or account != risk:
        raise DataQualityError('incomplete or conflicting positions')
    return {'count': len(account), 'NVDAUSDT': [
        {'side': side, 'quantity': quantity} for (symbol, side), quantity in account.items() if symbol == SYMBOL]}


def _open_orders(facts):
    identities = {(row['symbol'], row['clientOrderId']) for row in facts.open_orders}
    if (len(identities) != len(facts.open_orders)
            or any(not all(isinstance(value, str) and value for value in key) for key in identities)):
        raise DataQualityError('unusable open order identity')
    return {'count': len(identities), 'NVDAUSDT_count': sum(symbol == SYMBOL for symbol, _ in identities)}


def _quote(payload, fields):
    if not isinstance(payload, dict) or payload.get('symbol') != SYMBOL:
        raise DataQualityError('unexpected price identity')
    prices = _numbers(payload, fields, nonnegative=True)
    if min(prices.values()) <= 0:
        raise DataQualityError('unusable price')
    return dict(prices, time_utc=instant(payload['time']))


def _book(payload):
    prices = _quote(payload, ('bidPrice', 'askPrice'))
    if prices['bidPrice'] > prices['askPrice']:
        raise DataQualityError('crossed book')
    return prices


def _filter_rows(info):
    rows = info['filters']
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DataQualityError('malformed filters')
    filters = {row['filterType']: row for row in rows}
    if len(filters) != len(rows) or any(not isinstance(name, str) or not name for name in filters):
        raise DataQualityError('duplicate filters')
    return filters


def _symbol_values(info):
    keys = ('symbol', 'status', 'contractType', 'marginAsset', 'quoteAsset', 'orderTypes')
    values = {key: info.get(key) for key in keys}
    for key in keys[:-1]:
        if not isinstance(values[key], str) or not values[key]:
            values[key] = UNKNOWN
    order_types = values['orderTypes']
    if (not isinstance(order_types, list) or not order_types
            or any(not isinstance(kind, str) or not kind for kind in order_types)):
        values['orderTypes'] = UNKNOWN
    return values


def _filter_values(name, row):
    if name in FILTER_FIELDS:
        values = _numbers(row, FILTER_FIELDS[name], nonnegative=True)
        if (('minQty' in values and not 0 < values['minQty'] <= values['maxQty'])
                or ('maxPrice' in values and values['maxPrice'] and values['minPrice'] > values['maxPrice'])
                or ('multiplierDown' in values and not 0 < values['multiplierDown'] <= 1 <= values['multiplierUp'])):
            raise DataQualityError('conflicting filter limits')
        return values
    if name == 'MAX_NUM_ORDERS':
        if type(row.get('limit')) is not int or row['limit'] < 0:
            raise DataQualityError('invalid order limit')
        return {'limit': row['limit']}
    side = row.get('positionControlSide')
    if not isinstance(side, str) or not side:
        raise DataQualityError('invalid position control side')
    return {'positionControlSide': side}


def _bracket_values(observation):
    tiers = []
    for row in observation.rules.brackets:
        values = _numbers(row, ('notionalFloor', 'notionalCap', 'maintMarginRatio'), nonnegative=True)
        number, leverage = row['bracket'], row['initialLeverage']
        if (type(number) is not int or number < 1 or type(leverage) is not int or leverage < 1
                or not values['notionalFloor'] < values['notionalCap']):
            raise DataQualityError('invalid leverage bracket')
        tiers.append(dict(values, bracket=number, initialLeverage=leverage))
    tiers.sort(key=lambda tier: tier['notionalFloor'])
    if (not tiers or tiers[0]['notionalFloor'] != 0 or len({tier['bracket'] for tier in tiers}) != len(tiers)
            or any(left['notionalCap'] != right['notionalFloor'] for left, right in zip(tiers, tiers[1:]))):
        raise DataQualityError('incomplete or conflicting leverage brackets')
    coefficient = observation.rules.notional_coefficient
    return {'notionalCoef': 'NOT_REPORTED' if coefficient is None else coefficient, 'tiers': tiers}


def inspect_demo(credential=None, *, plan=None, limit_price=None, client_order_id=None, prepare_policy=None):
    """Return only the selected account facts, sanitized endpoint receipt and plan checks."""
    if client_order_id is not None:
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,36}', client_order_id):
            raise ValueError('explicit client order identity required')
    transport = _ReadReceipt(environment=DEMO, permit_account_read=True)
    client = FuturesClient(credential, environment=DEMO, transport=transport)
    checks = {}

    def add(name, source, value, eligible=None):
        checks[name] = dict(source=source, value=UNKNOWN if value is None else value, eligible=eligible)

    offset, time_error = _observe(client.sync_time)
    add('server_clock', '/fapi/v1/time', None if time_error else {'offset_ms': offset},
        None if time_error else True)
    info = rules = facts = account_error = rules_error = account_id = None

    def keep_symbol(row):
        nonlocal info
        info = row

    if credential is not None and not time_error:
        with closing(PaperStore(':memory:')) as store:
            adapter = FuturesAdapter(client, store, account_id=DEMO.account_id,
                                     clock=lambda: datetime.now(timezone.utc))
            account_id = adapter.account_id
            rules, rules_error = _observe(lambda: observe_demo_rules(
                adapter, SYMBOL, source_version=DEMO.host, on_symbol=keep_symbol))
            facts, account_error = _observe(adapter.read_facts)
    else:
        info, rules_error = _observe(lambda: read_demo_symbol(client, SYMBOL))

    if client_order_id is not None:
        identity = UNKNOWN
        if credential is not None and not time_error:
            try:
                client.request('GET', '/fapi/v1/order',
                               {'symbol': SYMBOL, 'origClientOrderId': client_order_id})
            except ExchangeError as error:
                if error.code == -2013:
                    identity = 'ABSENT'
            except (DataQualityError, OSError, TimeoutError):
                pass
            else:
                identity = 'PRESENT'
        add('order_identity', '/fapi/v1/order',
            {'clientOrderId': client_order_id, 'state': identity}, identity == 'ABSENT')

    mark, _ = _observe(lambda: _quote(client.request('GET', MARK, {'symbol': SYMBOL}, signed=False), ('markPrice',)))
    book, _ = _observe(lambda: _book(client.request('GET', BOOK, {'symbol': SYMBOL}, signed=False)))
    now = datetime.now(timezone.utc)
    # Evaluate each market observation at receipt, not against a future execution session.
    exchange_now = now + timedelta(milliseconds=offset or 0)
    for name, path, value in (('mark', MARK, mark), ('book', BOOK, book)):
        fresh = (None if value is None or time_error else
                 timedelta(0) <= exchange_now - value['time_utc'] <= timedelta(seconds=5))
        add(name, path, value, fresh)

    trading = public_filters = symbol_values = None
    order_types = ()
    if info is not None:
        symbol_values = _symbol_values(info)
        if UNKNOWN not in symbol_values.values():
            order_types = symbol_values['orderTypes']
            trading = (symbol_values['status'] == 'TRADING'
                       and symbol_values['marginAsset'] == symbol_values['quoteAsset'] == 'USDT'
                       and 'LIMIT' in order_types)
        public_filters, _ = _observe(lambda: _filter_rows(info))
    add('symbol', EXCHANGE, symbol_values, trading)
    public_filters = public_filters or {}
    for name in (*FILTER_FIELDS, 'MAX_NUM_ORDERS', 'POSITION_RISK_CONTROL'):
        value, _ = _observe(lambda: _filter_values(name, public_filters[name]))
        if name == 'POSITION_RISK_CONTROL' and name not in public_filters and public_filters:
            add(name, EXCHANGE, 'NOT_REPORTED', True)
        else:
            eligible = None if value is None else (
                value['positionControlSide'] == 'NONE' if name == 'POSITION_RISK_CONTROL'
                else value['limit'] > 0 if name == 'MAX_NUM_ORDERS' else True)
            add(name, EXCHANGE, value, eligible)
    unexpected = set(public_filters) - SUPPORTED_NETWORK_FILTERS
    add('unsupported_filters', EXCHANGE, sorted(unexpected) if public_filters else None,
        not unexpected if public_filters else None)

    brackets, _ = _observe(lambda: _bracket_values(rules)) if rules is not None else (None, None)
    add('leverage_brackets', BRACKETS, brackets,
        None if brackets is None else rules.rules.notional_coefficient in (None, Decimal(1)))
    signed_verified = facts is not None or rules is not None
    identity_source = ACCOUNT if facts is not None else BRACKETS
    add('account_id', identity_source, account_id if signed_verified else None, True if signed_verified else None)
    add('api_user_data', identity_source, 'VERIFIED' if signed_verified else None, True if signed_verified else None)
    add('api_key_trade', 'Demo key permissions', None)
    can_trade = facts.can_trade if facts else None
    can_trade_source = '/fapi/v2/account' if any(r['path'] == '/fapi/v2/account' for r in transport.endpoints) else ACCOUNT
    add('account_can_trade', can_trade_source, can_trade, can_trade)
    quant = facts.quantitative if facts else QuantitativeObservation(error=account_error or UNKNOWN)
    add('quantitative', STATUS, quant.report(SYMBOL),
        None if quant.error else not quant.applicable_locks(SYMBOL))
    mode = facts.hedge_mode if facts else None
    add('position_mode', MODE, None if mode is None else 'HEDGE' if mode else 'ONE_WAY',
        None if mode is None else not mode)

    funds, positions, orders, config = (None,) * 4
    if facts is not None:
        funds, _ = _observe(facts.usdt)
        positions, _ = _observe(lambda: _positions(facts))
        orders, _ = _observe(lambda: _open_orders(facts))
        config, _ = _observe(lambda: symbol_row(list(facts.symbol_config), SYMBOL))
    funds = None if funds is None else {key: funds[key] for key in (
        'walletBalance', 'availableBalance', 'unrealizedProfit', 'initialMargin')}
    margin_known = funds is not None and all(value is not None for value in funds.values())
    margin_ok = (funds['availableBalance'] > 0 and funds['initialMargin'] >= 0
                 and funds['walletBalance'] + funds['unrealizedProfit'] > 0) if margin_known else None
    add('usdt', ACCOUNT, None if funds is None else {
        key: UNKNOWN if value is None else value for key, value in funds.items()}, margin_ok)
    add('positions', f'{ACCOUNT} and {POSITION_RISK}', positions, positions['count'] == 0 if positions else None)
    add('open_orders', OPEN_ORDERS, orders, orders['count'] == 0 if orders else None)
    leverage = config.get('leverage') if config else None
    margin_type = config.get('marginType') if config else None
    add('leverage', CONFIG, leverage if type(leverage) is int and leverage > 0 else None,
        any(leverage <= row['initialLeverage'] for row in brackets['tiers'])
        if type(leverage) is int and leverage > 0 and brackets else None)
    add('margin_type', CONFIG, margin_type if margin_type in ('CROSSED', 'ISOLATED') else None,
        True if margin_type in ('CROSSED', 'ISOLATED') else None)

    sizing = {}
    for order_type in OrderType:
        if mark and order_type.value in order_types:
            candidate, error = _observe(lambda: minimum_opening_order(
                public_filters, order_type, mark_price=mark['markPrice'], limit_price=limit_price,
                quote_price=book['askPrice'] if checks['book']['eligible'] else None))
            sizing[order_type.value] = asdict(candidate) if candidate else error

    required = [check['eligible'] for name, check in checks.items() if name != 'api_key_trade']
    if credential is None or (not signed_verified and 'AUTH' in (account_error, rules_error)):
        status = 'P7_M02B_CREDENTIALS_NOT_CONFIGURED'
    elif all(value is not None for value in required):
        status = 'P7_M02B_DEMO_READ_ONLY_VERIFIED'
    else:
        status = 'DEMO_READ_ONLY_INCOMPLETE'
    plan_result = 'PENDING_APPROVED_PARAMETERS'
    if plan is not None:
        blocked = [name for name, check in checks.items()
                   if name != 'api_key_trade' and check['eligible'] is not True]
        if blocked:
            plan_result = {'status': 'BLOCKED_BY_READ_ONLY_CHECKS', 'checks': blocked}
        else:
            plan_result = review_opening_plan(plan, facts=facts, rules=rules.rules,
                mark_price=mark['markPrice'], quote_price=book['askPrice'], limit_price=limit_price, now=now)
    result = dict(status=status, environment=DEMO.name, host=DEMO.host, symbol=SYMBOL, observed_at=now,
                  safety=SAFETY_LABELS, checks=checks, account_read_error=account_error,
                  rule_read_error=rules_error, minimum_opening_orders=sizing, plan_review=plan_result,
                  execution_admission='BLOCKED', endpoints=transport.endpoints, network_writes=0)
    if prepare_policy is not None:
        result['bounded_preparation'] = prepare_policy(result, facts, rules.rules if rules else None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-dialog', action='store_true', help='Use the local masked Demo dialog.')
    args = parser.parse_args()
    if args.credential_dialog:
        credential = demo_credential_dialog()
        if credential is None:
            print('{"status":"CANCELLED"}')
            return
    else:
        try:
            credential = Credentials.from_env(DEMO)
        except ValueError:
            credential = None
    print(json.dumps(inspect_demo(credential), indent=2, default=str, ensure_ascii=True))


if __name__ == '__main__':
    main()
