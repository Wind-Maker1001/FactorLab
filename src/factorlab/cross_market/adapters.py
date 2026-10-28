"""Composable market rules, fees and corporate action evidence."""
from dataclasses import dataclass
import math

import pandas as pd
from factorlab.backtest.fees import FeeSchedule
from .contracts import ContractError, instant, session_date
from .calendar import SessionCalendar


@dataclass(frozen=True)
class AccountPolicy:
    account_type: str = 'cash'
    allow_day_trade: bool = True
    fractional_step: float = 1.0

    def __post_init__(self):
        if self.account_type != 'cash':
            raise ContractError('only fully funded cash accounts; margin/shorting unsupported')
        if type(self.allow_day_trade) is not bool or not math.isfinite(self.fractional_step) or self.fractional_step <= 0 or self.fractional_step > 1:
            raise ContractError('invalid account trading policy')


@dataclass(frozen=True)
class FeePeriod:
    start: str
    end: str
    commission_per_share: float = 0.0
    minimum: float = 0.0
    buy_bps: float = 0.0
    sell_bps: float = 0.0
    sell_per_share: float = 0.0
    sell_maximum: float = float('inf')

    def __post_init__(self):
        if session_date(self.start) > session_date(self.end):
            raise ContractError('invalid fee period')
        for name in ('commission_per_share', 'minimum', 'buy_bps', 'sell_bps', 'sell_per_share'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ContractError('negative/nonfinite fee')
        if math.isnan(self.sell_maximum) or self.sell_maximum < 0:
            raise ContractError('invalid fee maximum')


class DatedFees:
    """Explicit broker/regulatory scenarios, NOT a certified historical US tariff."""
    def __init__(self, periods, version):
        self.periods, self.version = tuple(sorted(periods, key=lambda p: p.start)), version
        for a, b in zip(self.periods, self.periods[1:]):
            if session_date(a.end) >= session_date(b.start):
                raise ContractError('overlapping fee periods')

    def charge(self, date, side, quantity, price):
        if side not in {'buy', 'sell'} or not all(math.isfinite(x) and x >= 0 for x in (quantity, price)):
            raise ContractError('invalid fee order')
        periods = [p for p in self.periods if session_date(p.start) <= session_date(date) <= session_date(p.end)]
        if len(periods) != 1:
            raise ContractError('fee history does not cover session')
        p = periods[0]
        if quantity == 0:
            return 0.0
        commission = max(p.minimum, p.commission_per_share * quantity)
        sidefee = quantity * price * (p.buy_bps if side == 'buy' else p.sell_bps) / 10000
        return commission + sidefee + (min(quantity * p.sell_per_share, p.sell_maximum) if side == 'sell' else 0)


class ChinaFees:
    def __init__(self, schedule=None):
        self.schedule = schedule or FeeSchedule.a_share_default()
        self.version = 'legacy-factorlab-A-share-scenario'

    def charge(self, date, side, quantity, price):
        return self.schedule.calculate(date, side, quantity * price).total


class USTradingRules:
    market = 'US'
    reuse_sale_proceeds = False

    def filter_research_observations(self, frame):
        return frame

    def __init__(self, account=None):
        self.account = account or AccountPolicy()

    def settlement_sessions(self, day):
        day = session_date(day)
        if day < pd.Timestamp('1995-06-07'):
            raise ContractError('US settlement rule history not implemented before 1995-06-07')
        return 1 if day >= pd.Timestamp('2024-05-28') else (2 if day >= pd.Timestamp('2017-09-05') else 3)

    def quantity(self, value, side, security):
        step = self.account.fractional_step
        return math.floor((max(0, value) + 1e-10) / step) * step

    def can_sell(self, acquired, day):
        return self.account.allow_day_trade or session_date(acquired) < session_date(day)

    def rejection(self, row, side):
        if pd.isna(row.get('halted')):
            return 'STATUS_UNKNOWN'
        return 'HALTED' if bool(row['halted']) else None


class ChinaTradingRules:
    market = 'CN'
    reuse_sale_proceeds = True

    def filter_research_observations(self, frame):
        if 'is_st' not in frame:
            raise ContractError('historical CN ST status unavailable')
        return frame.loc[frame.is_st.eq(False)]

    def settlement_sessions(self, day):
        return 1

    def quantity(self, value, side, security):
        # Full odd-lot liquidation is supported. STAR's 200-share minimum with
        # one-share increments is explicit; other IPO exceptions need evidence.
        value = max(0, value)
        if side == 'sell':
            return math.floor(value + 1e-10)
        board = security.get('board')
        if board is None and security.get('symbol'):
            from factorlab.backtest.rules import classify_board
            board = classify_board(security['symbol'])
        if board == 'star':
            return math.floor(value) if value >= 200 else 0
        return math.floor(value / 100) * 100

    def can_sell(self, acquired, day):
        return session_date(acquired) < session_date(day)

    def rejection(self, row, side):
        if pd.isna(row.get('halted')):
            return 'STATUS_UNKNOWN'
        if bool(row['halted']):
            return 'HALTED'
        # The provider must supply date-effective absolute bounds. Absence is
        # unknown, not proof of no limit (IPO reform/ST/board differences matter).
        key = 'limit_up' if side == 'buy' else 'limit_down'
        if row.get('no_price_limit') is True:
            return None
        bound = row.get(key)
        if bound is None or not math.isfinite(bound):
            return 'PRICE_LIMIT_EVIDENCE_MISSING'
        price = row['raw_open']
        if (side == 'buy' and price >= bound - 0.005) or (side == 'sell' and price <= bound + 0.005):
            return 'PRICE_LIMIT'
        return None


@dataclass
class MarketAdapter:
    market: str
    currency: str
    calendar: object
    universe: object
    rules: object
    fees: object
    corporate_actions: object
    settlement_calendar: object = None
    data: object = None

    def __post_init__(self):
        if self.calendar.market != self.market or self.rules.market != self.market:
            raise ContractError('mixed market adapter components')
        if self.settlement_calendar is None:
            self.settlement_calendar = self.calendar


class ChinaAshareAdapter(MarketAdapter):
    def __init__(self, calendar, universe, corporate_actions, fees=None, data=None):
        super().__init__('CN', 'CNY', calendar, universe, ChinaTradingRules(), fees or ChinaFees(), corporate_actions, data=data)


class USEquityAdapter(MarketAdapter):
    def __init__(self, calendar, universe, corporate_actions, fees, account=None, settlement_calendar=None, data=None):
        if settlement_calendar is None:
            # Conservative US cash settlement sessions: exchange sessions that
            # are also federal banking business days. Injectable source schedule
            # is required for certified settlement history and emergency changes.
            from pandas.tseries.holiday import USFederalHolidayCalendar
            holidays = USFederalHolidayCalendar().holidays(calendar.coverage_start, calendar.coverage_end)
            schedule = calendar.schedule.loc[~calendar.schedule.index.isin(holidays)]
            settlement_calendar = SessionCalendar(schedule, 'US', calendar.timezone, calendar.version + ':federal-bank-intersection-scenario')
        super().__init__('US', 'USD', calendar, universe, USTradingRules(account), fees, corporate_actions, settlement_calendar, data)


class ActionLedger:
    """Explicit event coverage plus bitemporal action records.

    Empty evidence is accepted only within a declared covered interval and ID
    set. Synthetic fixtures declare completeness; real evidence cannot be
    certified by this object or by a checksum.
    """
    COLUMNS = ['event_id', 'instrument_id', 'kind', 'effective_date', 'available_at', 'ratio', 'cash_per_share', 'pay_date', 'target_id', 'evidence']
    KINDS = {'split', 'reverse_split', 'cash_dividend', 'symbol_change', 'delisting', 'merger',
             'bonus_share', 'capitalization', 'rights_issue', 'identity_change'}

    def __init__(self, events, *, coverage_start, coverage_end, instrument_ids, version):
        self.frame = events.copy()
        self.start, self.end = session_date(coverage_start), session_date(coverage_end)
        self.ids, self.version = frozenset(instrument_ids), version
        if self.start > self.end or not self.ids or not version:
            raise ContractError('explicit corporate action coverage required')
        if set(self.COLUMNS) - set(events):
            raise ContractError('corporate action fields missing')
        if events.event_id.duplicated().any() or not set(events.kind) <= self.KINDS:
            raise ContractError('duplicate/unsupported corporate action')
        self.frame['effective_date'] = pd.to_datetime([session_date(x) for x in events.effective_date])
        self.frame['available_at'] = pd.to_datetime([instant(x) for x in events.available_at], utc=True)
        self.frame['pay_date'] = pd.to_datetime(events.pay_date)
        for r in self.frame.itertuples():
            if r.instrument_id not in self.ids or not isinstance(r.evidence, str) or not r.evidence:
                raise ContractError('action evidence or identity missing')
            if r.kind in {'split', 'reverse_split', 'merger', 'bonus_share', 'capitalization', 'rights_issue', 'identity_change'} and (not math.isfinite(r.ratio) or r.ratio <= 0):
                raise ContractError('invalid action ratio')
            if r.kind in {'cash_dividend', 'merger', 'delisting'} and (not math.isfinite(r.cash_per_share) or r.cash_per_share < 0):
                raise ContractError('cash consideration must be explicit, including zero')
            if r.kind == 'cash_dividend' and (pd.isna(r.pay_date) or r.pay_date < r.effective_date):
                raise ContractError('dividend payment precedes entitlement')
            if r.kind in {'merger', 'identity_change'} and (r.target_id not in self.ids or r.target_id == r.instrument_id):
                raise ContractError('merger target identity required')
            if r.kind == 'identity_change' and (r.ratio != 1 or r.cash_per_share != 0):
                raise ContractError('identity change must preserve quantity and cash')
            if r.kind in {'bonus_share', 'capitalization', 'rights_issue'}:
                # Later delivery needs a separate, date-specific entitlement
                # valuation source. It must not be marked using a stale scalar.
                if not hasattr(r, 'quantity_date') or pd.isna(r.quantity_date):
                    raise ContractError('share delivery date required')
                if session_date(r.quantity_date) < r.effective_date:
                    raise ContractError('share delivery precedes entitlement')
            if r.kind == 'rights_issue':
                policy = getattr(r, 'rights_policy', None)
                if policy not in {'exercise', 'sell', 'expire'}:
                    raise ContractError('explicit rights policy required; unknown is not expiry')
                if not math.isfinite(r.cash_per_share) or r.cash_per_share < 0:
                    raise ContractError('explicit rights cash consideration required')
                if policy == 'exercise' and (not hasattr(r, 'subscription_price') or not math.isfinite(r.subscription_price) or r.subscription_price < 0 or r.cash_per_share != 0):
                    raise ContractError('explicit subscription price required')
                if policy == 'expire' and r.cash_per_share != 0:
                    raise ContractError('expired rights cannot pay cash')
                if policy == 'sell' and (pd.isna(r.pay_date) or r.pay_date < r.effective_date):
                    raise ContractError('rights sale payment date required after entitlement')

    def require_coverage(self, start, end, instrument_ids):
        if session_date(start) < self.start or session_date(end) > self.end or set(instrument_ids) - self.ids:
            raise ContractError('CORPORATE_ACTION_COVERAGE_UNKNOWN')

    def events(self, start, end, as_of):
        f = self.frame
        return f.loc[(f.effective_date >= session_date(start)) & (f.effective_date <= session_date(end)) &
                     (f.available_at <= instant(as_of))].sort_values(['effective_date', 'event_id']).copy()


class UnknownActions:
    def require_coverage(self, *args):
        raise ContractError('CORPORATE_ACTION_COVERAGE_UNKNOWN: no trusted complete event ledger')


def legacy_csi500_backtest(*args, execution=False, **kwargs):
    """Compatibility bridge delegates unchanged original algorithms/contracts."""
    from factorlab.backtest import run_fast_backtest, run_execution_backtest
    return (run_execution_backtest if execution else run_fast_backtest)(*args, **kwargs)
