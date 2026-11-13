"""Versioned bounded BUY LIMIT candidates; policy data never grants write authority."""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
import hashlib
import json

from factorlab.trading.contracts import utc, Market, OrderIntent, OrderType, Side
from .protocol import SafetyBlocked


def _canonical(value):
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Decimal):
        text = format(value, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    if isinstance(value, datetime):
        from datetime import timezone
        utc(value)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    return value


def canonical_json(value):
    return json.dumps(_canonical(value), sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True, kw_only=True)
class BoundedQuotePolicy:
    account_id: str
    client_order_id: str
    quantity: Decimal
    offset_bps: Decimal
    min_price: Decimal
    max_price: Decimal
    max_ask: Decimal
    max_notional: Decimal
    max_fee: Decimal
    max_funds: Decimal
    fee_rate: Decimal
    expires_at: datetime
    version: int = 1
    max_quote_age_seconds: int = 5
    environment: str = 'DEMO'
    symbol: str = 'NVDAUSDT'
    side: str = 'BUY'
    order_type: str = 'LIMIT'
    time_in_force: str = 'GTC'
    position_side: str = 'BOTH'
    reduce_only: bool = False

    def __post_init__(self):
        import re
        utc(self.expires_at)
        if ((self.environment, self.symbol, self.side, self.order_type, self.time_in_force, self.position_side)
                != ('DEMO', 'NVDAUSDT', 'BUY', 'LIMIT', 'GTC', 'BOTH') or self.reduce_only is not False):
            raise ValueError('bounded Demo BUY LIMIT contract required')
        if type(self.max_quote_age_seconds) is not int or not 0 < self.max_quote_age_seconds <= 5:
            raise ValueError('quote freshness must be at most five seconds')
        if self.version != 1 or type(self.version) is not int:
            raise ValueError('unsupported bounded quote version')
        if not re.fullmatch(r'DEMO:FactorLab-Demo:[0-9a-f]{64}', self.account_id):
            raise ValueError('credential-bound Demo account required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,36}', self.client_order_id):
            raise ValueError('exact client ID required')
        for value in (self.quantity, self.offset_bps, self.min_price, self.max_price,
                      self.max_ask, self.max_notional, self.max_fee, self.max_funds, self.fee_rate):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError('positive finite Decimal bounds required')
        if self.offset_bps >= 10000 or self.min_price > self.max_price:
            raise ValueError('invalid quote range')

    def to_json(self):
        return canonical_json(self)

    @property
    def digest(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class BoundedQuoteObservation:
    account_id: str
    observed_at: datetime
    mark: Decimal
    bid: Decimal
    ask: Decimal
    rules_json: str
    account_json: str
    mark_at: datetime
    book_at: datetime

    def __post_init__(self):
        for stamp in (self.observed_at, self.mark_at, self.book_at):
            utc(stamp)
        for value in (self.mark, self.bid, self.ask):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError('positive Decimal observation required')
        if self.bid > self.ask:
            raise ValueError('uncrossed book required')

    @classmethod
    def capture(cls, *, facts, rules, observed_at, mark, bid, ask, mark_at, book_at):
        return cls(account_id=facts.account_id, observed_at=observed_at, mark=mark, bid=bid, ask=ask,
                   rules_json=canonical_json(rules), account_json=canonical_json(facts),
                   mark_at=mark_at, book_at=book_at)


def bounded_quote(plan, observation, *, facts, rules, now):
    """Return one exact intent and a sanitized receipt, or reject without clipping."""
    from .plan_review import review_opening_plan
    policy = plan.quote_policy
    utc(now)
    receipt = dict(policy_digest=policy.digest, plan_digest=hashlib.sha256(canonical_json(plan).encode()).hexdigest(),
                   client_order_id=policy.client_order_id, observed_at=observation.observed_at.isoformat(),
                   age_seconds=str((now - observation.observed_at).total_seconds()),
                   mark=str(observation.mark), bid=str(observation.bid), ask=str(observation.ask),
                   bounds=json.loads(policy.to_json()), dispatch='NOT_DISPATCHED', rejection=None)
    tick = Decimal(rules.filters['PRICE_FILTER']['tickSize'])
    price = (observation.bid * (1 - policy.offset_bps / 10000) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    band = rules.filters['PERCENT_PRICE']
    low, high = observation.mark * Decimal(band['multiplierDown']), observation.mark * Decimal(band['multiplierUp'])
    notional = policy.quantity * max(price, observation.ask, observation.mark)
    fee = notional * policy.fee_rate
    funds = notional + fee
    receipt.update(candidate=str(price), mark_band=[str(low), str(high)],
                   actual_notional=str(policy.quantity * price), notional=str(notional), fee=str(fee), funds=str(funds),
                   evaluated_at=now.isoformat(), failed_condition=None,
                   source_times=dict(mark=observation.mark_at.isoformat(), book=observation.book_at.isoformat()),
                   source_ages=dict(mark=str((now-observation.mark_at).total_seconds()),
                                    book=str((now-observation.book_at).total_seconds())))

    def reject(code, condition):
        receipt['rejection'] = code
        receipt['failed_condition'] = condition
        error = SafetyBlocked(code)
        error.receipt = receipt
        raise error
    if policy.expires_at <= now or plan.expires_at <= now:
        reject('QUOTE_EXPIRED', 'now < policy.expires_at and now < plan.expires_at')
    if any(not timedelta(0) <= now - stamp <= timedelta(seconds=policy.max_quote_age_seconds)
           for stamp in (observation.observed_at, observation.mark_at, observation.book_at)):
        reject('STALE_QUOTE', '0 <= observation/mark/book age <= max_quote_age_seconds')
    if observation.account_id != policy.account_id or facts.account_id != policy.account_id:
        reject('QUOTE_ACCOUNT_MISMATCH', 'observation.account_id == facts.account_id == policy.account_id')
    if observation.rules_json != canonical_json(rules) or observation.account_json != canonical_json(facts):
        reject('QUOTE_CONTEXT_MISMATCH', 'captured rules/account == current execution rules/account')
    if not policy.min_price <= price <= policy.max_price or price >= observation.bid:
        reject('QUOTE_PRICE_BOUNDS', 'min_price <= candidate <= max_price and candidate < bid')
    if observation.ask > policy.max_ask:
        reject('QUOTE_ASK_LIMIT', 'ask <= max_ask')
    if not low <= price <= high or not low <= observation.ask <= high:
        reject('QUOTE_MARK_BAND', 'mark_band_low <= candidate/ask <= mark_band_high')
    if notional > policy.max_notional or fee > policy.max_fee or funds > policy.max_funds:
        reject('QUOTE_BUDGET', 'notional <= max_notional and fee <= max_fee and funds <= max_funds')
    balances = facts.usdt()
    if funds > balances['availableBalance'] or balances['initialMargin'] + funds > plan.max_margin:
        reject('QUOTE_FUNDS', 'funds <= availableBalance and initialMargin + funds <= max_margin')
    review = review_opening_plan(plan, facts=facts, rules=rules, mark_price=observation.mark,
        quote_price=max(observation.ask, observation.mark), limit_price=price, now=now,
        fixed_quantity=policy.quantity)
    if review['LIMIT']:
        reject('|'.join(review['LIMIT']), 'official fixed-quantity opening review has no blockers')
    intent = OrderIntent(Market.BINANCE_USDM, plan.symbol, Side.BUY, OrderType.LIMIT,
                         policy.quantity, price, policy.client_order_id)
    return intent, receipt
