"""Observed current exchange filters/brackets; not historical trading rules."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import lcm
import re

from factorlab.integrations.binance.market import _decimal, symbol_name
from factorlab.trading import Market, OrderType, RiskDecision, Side
from factorlab.trading.contracts import utc
from factorlab.integrations.binance_equity_trading.protocol import Unavailable

D = _decimal


def market_quantity_constraints(symbol_info):
    """Return the common market-order quantity grid and intersected bounds."""
    filters = {f['filterType']: f for f in symbol_info.get('filters', ())}
    lots = [filters.get(name) for name in ('LOT_SIZE', 'MARKET_LOT_SIZE')]
    if any(f is None or any(f.get(k) is None for k in ('stepSize', 'minQty', 'maxQty'))
           for f in lots):
        raise Unavailable('UNKNOWN_QUANTITY_RULE')
    steps = [D(f['stepSize']) for f in lots if D(f['stepSize']) > 0]
    step = Decimal(0)
    if steps:
        exponent = min(s.as_tuple().exponent for s in steps)
        integers = []
        for value in steps:
            parts = value.as_tuple()
            coefficient = int(''.join(map(str, parts.digits)))
            integers.append(coefficient * 10 ** (parts.exponent - exponent))
        common = lcm(*integers)
        step = Decimal((0, tuple(map(int, str(common))), exponent))
    return step, max(D(f['minQty']) for f in lots), min(D(f['maxQty']) for f in lots)


@dataclass(frozen=True)
class RuleObservation:
    symbol: str
    filters: dict
    brackets: tuple[dict, ...]
    observed_at: datetime
    source_version: str
    notional_coefficient: Decimal | None = None
    order_types: tuple[str, ...] | None = None

    @classmethod
    def from_exchange(cls, symbol_info, bracket_info, observed_at, source_version):
        utc(observed_at)
        if (any(symbol_info.get(k) is None for k in ('symbol', 'status', 'quoteAsset', 'marginAsset', 'filters'))
                or bracket_info is None or any(bracket_info.get(k) is None for k in ('symbol', 'brackets'))):
            raise Unavailable('INCOMPLETE_FUTURES_RULES')
        if not source_version or symbol_info['symbol'] != bracket_info['symbol']:
            raise ValueError('matching source identities and source version required')
        if symbol_info['status'] != 'TRADING' or symbol_info['quoteAsset'] != 'USDT' or symbol_info['marginAsset'] != 'USDT':
            raise ValueError('only currently trading USDT-margined linear contracts supported')
        return cls(symbol_name(symbol_info['symbol']), {f['filterType']: dict(f) for f in symbol_info['filters']},
                   tuple(dict(b) for b in bracket_info['brackets']), observed_at, source_version,
                   None if 'notionalCoef' not in bracket_info else D(bracket_info['notionalCoef']),
                   None if 'orderTypes' not in symbol_info else tuple(symbol_info['orderTypes']))

    def validate(self, intent, *, mark_price, leverage, available_margin, current_notional,
                 now, max_leverage=1, max_age_seconds=300, remaining_quantity=None):
        utc(now)
        snapshot = {'source_version': self.source_version, 'observed_at': self.observed_at.isoformat(),
                    'historical': False, 'max_leverage': max_leverage}
        def decision(reason):
            return RiskDecision(reason == 'APPROVED', reason, snapshot)
        if not timedelta(0) <= now - self.observed_at <= timedelta(seconds=max_age_seconds):
            return decision('STALE_RULES')
        if self.order_types is None:
            return decision('UNKNOWN_ORDER_TYPES')
        if intent.order_type.value not in self.order_types:
            return decision('UNSUPPORTED_ORDER_TYPE')
        if self.notional_coefficient not in (None, Decimal('1')):
            return decision('CUSTOM_NOTIONAL_COEFFICIENT_UNVERIFIED')
        if intent.market != Market.BINANCE_USDM or intent.instrument_id != self.symbol:
            return decision('INSTRUMENT_MISMATCH')
        if type(leverage) is not int or not 1 <= leverage <= max_leverage:
            return decision('LEVERAGE_LIMIT')
        for value in (mark_price, available_margin, current_notional):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                return decision('UNKNOWN_ACCOUNT_OR_MARK')
        if not mark_price:
            return decision('UNKNOWN_ACCOUNT_OR_MARK')
        lot_name = 'MARKET_LOT_SIZE' if intent.order_type == OrderType.MARKET else 'LOT_SIZE'
        lot = self.filters.get(lot_name)
        # MARKET_LOT_SIZE and LOT_SIZE are both applicable to market quantities.
        for f in (lot, self.filters.get('LOT_SIZE')):
            if f is None or any(f.get(k) is None for k in ('stepSize', 'minQty', 'maxQty')):
                return decision('UNKNOWN_QUANTITY_RULE')
            step = D(f['stepSize'])
            if intent.quantity < D(f['minQty']) or intent.quantity > D(f['maxQty']) or (step and intent.quantity % step):
                return decision('QUANTITY_FILTER')
        if intent.limit_price is not None:
            f = self.filters.get('PRICE_FILTER')
            if f is None or any(f.get(k) is None for k in ('tickSize', 'minPrice', 'maxPrice')):
                return decision('UNKNOWN_PRICE_RULE')
            price, step = intent.limit_price, D(f['tickSize'])
            if ((D(f['minPrice']) and price < D(f['minPrice'])) or
                (D(f['maxPrice']) and price > D(f['maxPrice'])) or (step and price % step)):
                return decision('PRICE_FILTER')
        notional = intent.quantity * (intent.limit_price or mark_price)
        minimum = self.filters.get('MIN_NOTIONAL')
        if minimum is None or minimum.get('notional') is None:
            return decision('UNKNOWN_NOTIONAL_RULE')
        if not intent.reduce_only and notional < D(minimum['notional']):
            return decision('MIN_NOTIONAL')
        risk_notional = (intent.quantity if remaining_quantity is None else remaining_quantity) * (intent.limit_price or mark_price)
        projected = max(Decimal('0'), current_notional - risk_notional) if intent.reduce_only else current_notional + risk_notional
        bracket = next((b for b in self.brackets if D(b['notionalFloor']) <= projected < D(b['notionalCap'])), None)
        if bracket is None or leverage > int(bracket['initialLeverage']):
            return decision('RISK_BRACKET_LIMIT')
        if not intent.reduce_only and risk_notional / leverage > available_margin:
            return decision('INSUFFICIENT_MARGIN')
        return decision('APPROVED')


def observe_rules(public_client, signed_client, symbol, observed_at, source_version):
    """Reuse existing PublicClient; no market-data download or second store."""
    symbol_name(symbol)
    info = public_client.get('exchangeInfo')
    row = next(r for r in info['symbols'] if r['symbol'] == symbol)
    brackets = signed_client.request('GET', '/fapi/v1/leverageBracket', {'symbol': symbol})
    bracket = next(r for r in brackets if r['symbol'] == symbol)
    return RuleObservation.from_exchange(row, bracket, observed_at, source_version)


def current_tier(rules, exchange_notional):
    """A tier selected only from exchange-reported notional and observed rules."""
    if exchange_notional is None or rules.notional_coefficient not in (None, Decimal('1')):
        return None
    return next((dict(b) for b in rules.brackets
                 if D(b['notionalFloor']) <= abs(exchange_notional) < D(b['notionalCap'])), None)


def order_parameters(intent, *, hedge_mode=False, hedge_side=None):
    if intent.market != Market.BINANCE_USDM:
        raise ValueError('USD-M intent required')
    symbol_name(intent.instrument_id)
    if not re.fullmatch(r'[.A-Za-z0-9_:/-]{1,36}', intent.client_order_id):
        raise ValueError('Binance clientOrderId must match exchange format, length <=36')
    if hedge_mode:
        if hedge_side not in {'LONG', 'SHORT'} or intent.reduce_only:
            raise ValueError('Hedge Mode requires LONG/SHORT and forbids reduceOnly')
        position_side = hedge_side
    else:
        if hedge_side is not None:
            raise ValueError('one-way mode forbids LONG/SHORT positionSide')
        position_side = 'BOTH'
    result = dict(symbol=intent.instrument_id, side=intent.side.value, type=intent.order_type.value,
                  quantity=format(intent.quantity, 'f'), newClientOrderId=intent.client_order_id,
                  positionSide=position_side, newOrderRespType='RESULT')
    if not hedge_mode:
        result['reduceOnly'] = 'true' if intent.reduce_only else 'false'
    if intent.order_type == OrderType.LIMIT:
        result.update(price=format(intent.limit_price, 'f'), timeInForce=intent.time_in_force)
    return result
