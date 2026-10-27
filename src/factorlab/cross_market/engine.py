"""One market-neutral target-weight engine and explicit cash/action accounting."""
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd
from factorlab.portfolio.topk import select_top_k
from .contracts import ContractError, ValuationError, instant, session_date


def target_weights(scores, calendar, top_k=20, rebalance=5):
    f = scores.rename(columns={'instrument_id': 'symbol'})
    result = select_top_k(f, top_k=top_k, rebalance_every=rebalance,
                          trading_dates=calendar.schedule.index, minimum_cross_section=top_k)
    result = result.rename(columns={'symbol': 'instrument_id'})
    if 'signal_at' in scores:
        result = result.merge(scores[['date', 'instrument_id', 'signal_at']], on=['date', 'instrument_id'], how='left', validate='one_to_one')
    else:
        result['signal_at'] = result.date.map(calendar.schedule.close)
    return result


@dataclass
class BacktestResult:
    nav: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejections: pd.DataFrame
    actions: pd.DataFrame
    return_definition: str


class ExecutionEngine:
    """Daily core-session long-only simulation. Orders use previous-close targets.

    Every purchase uses trading-available cash. US cash proceeds remain locked
    until settlement; fully settled-funded shares may be sold on trade day when
    the account policy permits. Settlement and share sellability are independent.
    Intraday order queues, market impact and actual fills are outside this model.
    """
    def __init__(self, adapter, initial_cash=1_000_000):
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ContractError('positive finite initial cash required')
        self.adapter = adapter
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.pending = []
        self.receivables = []
        self.lots = {}
        self.trades, self.rejections, self.action_log = [], [], []
        self._used = False

    def quantity(self, iid):
        return sum(l['quantity'] for l in self.lots.get(iid, []))

    def cost_basis(self, iid):
        return sum(l['basis'] for l in self.lots.get(iid, []))

    def settle(self, day):
        self.pending = [p for p in self.pending if p['due'] > day]
        due = [p for p in self.receivables if p['due'] <= day]
        self.cash += sum(p['amount'] for p in due)
        self.receivables = [p for p in self.receivables if p['due'] > day]

    def available_cash(self):
        locked = 0 if self.adapter.rules.reuse_sale_proceeds else sum(p['amount'] for p in self.pending)
        return max(0.0, self.cash - locked)

    def settlement_date(self, day):
        n = self.adapter.rules.settlement_sessions(day)
        dates = self.adapter.settlement_calendar.schedule.index
        i = dates.searchsorted(day, side='right') + n - 1
        if i >= len(dates):
            raise ContractError('settlement calendar coverage insufficient')
        return dates[i]

    def order(self, iid, side, quantity, row, day, security):
        if side not in {'buy', 'sell'} or not math.isfinite(quantity) or quantity < 0:
            raise ContractError('invalid order quantity/side')
        rule = self.adapter.rules
        reason = rule.rejection(row, side)
        price = row.get('raw_open')
        if price is None or not math.isfinite(price) or price <= 0:
            reason = 'PRICE_MISSING'
        if reason:
            self.rejections.append(dict(date=day, instrument_id=iid, side=side, reason=reason))
            return 0.0
        quantity = rule.quantity(quantity, side, security)
        lots = self.lots.setdefault(iid, [])
        if side == 'sell':
            sellable = sum(l['quantity'] for l in lots if rule.can_sell(l['date'], day))
            if quantity > sellable + 1e-8:
                self.rejections.append(dict(date=day, instrument_id=iid, side=side, reason='SHARES_NOT_SELLABLE'))
                quantity = min(quantity, sellable)
        if quantity <= 1e-10:
            return 0.0
        fee = self.adapter.fees.charge(day, side, quantity, price)
        if side == 'buy' and quantity * price + fee > self.available_cash() + 1e-8:
            affordable = max(0, (self.available_cash() - fee) / price)
            quantity = rule.quantity(min(quantity, affordable), side, security)
            fee = self.adapter.fees.charge(day, side, quantity, price)
            # A minimum commission can change at the rounded quantity boundary.
            if quantity * price + fee > self.available_cash() + 1e-8:
                quantity = 0
            self.rejections.append(dict(date=day, instrument_id=iid, side=side, reason='CASH_OR_SETTLEMENT_CONSTRAINT'))
        if quantity <= 1e-10:
            return 0.0
        if side == 'sell' and self.cash + quantity * price - fee < -1e-8:
            self.rejections.append(dict(date=day, instrument_id=iid, side=side, reason='INSUFFICIENT_CASH_FOR_FEES'))
            return 0.0
        released_basis = 0.0
        if side == 'buy':
            self.cash -= quantity * price + fee
            lots.append(dict(quantity=quantity, basis=quantity * price + fee, date=day))
        else:
            remaining = quantity
            for lot in lots:
                if not rule.can_sell(lot['date'], day) or lot['quantity'] <= 0:
                    continue
                taken = min(remaining, lot['quantity'])
                basis = lot['basis'] * taken / lot['quantity']
                lot['quantity'] -= taken
                lot['basis'] -= basis
                remaining -= taken
                released_basis += basis
                if remaining <= 1e-10:
                    break
            proceeds = quantity * price - fee
            self.cash += proceeds
            self.pending.append(dict(due=self.settlement_date(day), amount=proceeds))
        if self.cash < -1e-6:
            raise ContractError('cash accounting invariant violated')
        self.trades.append(dict(date=day, execution_at=self.adapter.calendar.at(day, 'open'),
            instrument_id=iid, side=side, quantity=quantity, price=price, fee=fee,
            released_basis=released_basis, realized_pnl=(quantity * price - fee - released_basis) if side == 'sell' else 0.0,
            cash=self.cash, available_cash=self.available_cash()))
        return quantity

    def apply_actions(self, day, prices):
        ledger = self.adapter.corporate_actions
        # Knowledge-time filtering must not silently hide an effective action.
        all_today = ledger.frame.loc[ledger.frame.effective_date == day]
        if (all_today.available_at > self.adapter.calendar.at(day, 'open')).any():
            raise ContractError('corporate action was unavailable at effective execution time')
        before_session = {iid:self.quantity(iid) for iid in all_today.instrument_id}
        # Per-share cash/rights amounts use PRE-SESSION quantities. Multiple
        # transformations of one identity need an explicit ordering contract.
        transforms = all_today.loc[all_today.kind.isin({'split','reverse_split','bonus_share','capitalization','rights_issue','merger','identity_change','delisting'})]
        if transforms.instrument_id.duplicated().any():
            raise ContractError('ambiguous simultaneous share actions require an ordered event contract')
        for r in all_today.sort_values('event_id').itertuples():
            quantity, basis = self.quantity(r.instrument_id), self.cost_basis(r.instrument_id)
            cash_before = self.cash
            receivable_before = sum(p['amount'] for p in self.receivables)
            if r.kind in {'split', 'reverse_split'}:
                for lot in self.lots.get(r.instrument_id, []):
                    lot['quantity'] *= r.ratio
            elif r.kind == 'cash_dividend' and before_session[r.instrument_id]:
                amount = before_session[r.instrument_id] * r.cash_per_share
                if r.pay_date <= day:
                    self.cash += amount
                else:
                    self.receivables.append(dict(due=r.pay_date, amount=amount))
            elif r.kind in {'bonus_share', 'capitalization'} and quantity:
                if session_date(r.quantity_date) != day:
                    raise ContractError('CORPORATE_ACTION_ENTITLEMENT_VALUATION_UNKNOWN: delayed share delivery')
                for lot in self.lots.get(r.instrument_id, []):
                    lot['quantity'] *= 1 + r.ratio
            elif r.kind == 'rights_issue' and quantity:
                policy = r.rights_policy
                entitlement = before_session[r.instrument_id] * r.ratio
                if policy == 'exercise':
                    if session_date(r.quantity_date) != day:
                        raise ContractError('CORPORATE_ACTION_ENTITLEMENT_VALUATION_UNKNOWN: delayed rights delivery')
                    cost = entitlement * r.subscription_price
                    if cost > self.available_cash() + 1e-8:
                        raise ContractError('RIGHTS_SUBSCRIPTION_UNFUNDED: explicit alternative policy required')
                    self.cash -= cost
                    self.lots.setdefault(r.instrument_id, []).append(dict(quantity=entitlement,basis=cost,date=day))
                elif policy == 'sell':
                    # Explicit net rights-sale proceeds per original share,
                    # supported by event evidence; never guessed from raw gaps.
                    amount = before_session[r.instrument_id] * r.cash_per_share
                    if r.pay_date <= day:
                        self.cash += amount
                    else:
                        self.receivables.append(dict(due=r.pay_date, amount=amount))
                elif policy != 'expire':
                    raise ContractError('RIGHTS_POLICY_UNKNOWN')
            elif r.kind == 'delisting' and quantity:
                self.cash += quantity * r.cash_per_share
                self.lots[r.instrument_id] = []
            elif r.kind in {'merger', 'identity_change'} and quantity:
                if r.target_id not in prices.index or not math.isfinite(prices.loc[r.target_id, 'raw_open']) or prices.loc[r.target_id, 'raw_open'] <= 0:
                    raise ContractError('merger target valuation missing')
                stock_value = quantity * r.ratio * prices.loc[r.target_id, 'raw_open']
                cash_value = quantity * r.cash_per_share
                target_basis = basis * stock_value / (stock_value + cash_value) if stock_value + cash_value else 0
                for lot in self.lots.get(r.instrument_id, []):
                    self.lots.setdefault(r.target_id, []).append(dict(quantity=lot['quantity'] * r.ratio,
                        basis=target_basis * lot['quantity'] / quantity, date=lot['date']))
                self.cash += cash_value
                self.lots[r.instrument_id] = []
            self.action_log.append(dict(date=day, event_id=r.event_id, instrument_id=r.instrument_id, kind=r.kind,
                quantity_before=quantity, quantity_after=self.quantity(r.instrument_id), basis_before=basis,
                basis_after=self.cost_basis(r.instrument_id), cash_per_share=r.cash_per_share,
                cash_change=self.cash-cash_before, receivable_change=sum(p['amount'] for p in self.receivables)-receivable_before,
                source_version=getattr(r,'source_version',ledger.version), evidence_status=getattr(r,'evidence_status','DECLARED_V1'),
                target_id=r.target_id, target_quantity_after=self.quantity(r.target_id) if isinstance(r.target_id,str) else 0.))

    def run(self, bars, targets, start, end):
        if self._used:
            raise ContractError('execution engines are single-run accounting objects')
        self._used = True
        if missing := {'date', 'instrument_id', 'raw_open', 'raw_close', 'halted'} - set(bars):
            raise ContractError('execution data missing: ' + str(missing))
        if bars.duplicated(['date', 'instrument_id']).any():
            raise ContractError('duplicate execution bars')
        sessions = self.adapter.calendar.sessions(start, end)
        ids = sorted(set(bars.instrument_id) | set(targets.instrument_id))
        self.adapter.corporate_actions.require_coverage(start, end, ids)
        orders = {}
        for day, group in targets.groupby('date'):
            if group.instrument_id.duplicated().any() or not np.isfinite(group.target_weight).all() or (group.target_weight < 0).any() or group.target_weight.sum() > 1 + 1e-8:
                raise ContractError('invalid long-only target weights')
            next_day = self.adapter.calendar.shift(day, 1)
            signal_at = self.adapter.calendar.at(day, 'close')
            if 'signal_at' in group and (pd.to_datetime(group.signal_at, utc=True) > signal_at).any():
                raise ContractError('signal was unavailable at declared signal close')
            if signal_at >= self.adapter.calendar.at(next_day, 'open'):
                raise ContractError('noncausal target execution')
            orders[next_day] = group.set_index('instrument_id').target_weight.to_dict()
        groups = {d: f.set_index('instrument_id') for d, f in bars.groupby('date')}
        nav, positions = [], []
        for day in sessions.index:
            self.settle(day)
            prices = groups.get(day, pd.DataFrame(columns=['raw_open', 'raw_close']))
            if hasattr(self.adapter.corporate_actions, 'require_session'):
                self.adapter.corporate_actions.require_session(day, ids, self.adapter.calendar.at(day, 'open'))
            self.apply_actions(day, prices)
            held = [iid for iid in self.lots if self.quantity(iid) > 1e-10]
            for iid in held:
                if iid not in prices.index or not np.isfinite(prices.loc[iid, ['raw_open', 'raw_close']].astype(float)).all() or (prices.loc[iid, ['raw_open', 'raw_close']].astype(float) <= 0).any():
                    raise ContractError('held instrument valuation missing; no stale-price invention: ' + iid)
            receivable = sum(p['amount'] for p in self.receivables)
            open_nav = self.cash + receivable + sum(self.quantity(iid) * prices.loc[iid, 'raw_open'] for iid in held)
            if day in orders:
                weights = orders[day]
                for iid in weights:
                    if iid not in prices.index or not np.isfinite(prices.loc[iid, ['raw_open','raw_close']].astype(float)).all() or (prices.loc[iid, ['raw_open','raw_close']].astype(float) <= 0).any():
                        raise ContractError('target instrument valuation missing: ' + iid)
                eligible = self.adapter.universe.members(day, self.adapter.calendar.at(day, 'open')).set_index('instrument_id')
                desired = {}
                for iid in sorted(set(held) | set(weights)):
                    if iid not in prices.index:
                        self.rejections.append(dict(date=day, instrument_id=iid, reason='PRICE_MISSING'))
                        continue
                    desired[iid] = open_nav * weights.get(iid, 0) / prices.loc[iid, 'raw_open']
                # Liquidations precede purchases. Sellability and cash availability
                # are checked independently for every fill.
                for side in ('sell', 'buy'):
                    for iid, target in desired.items():
                        delta = target - self.quantity(iid)
                        if (side == 'sell' and delta >= -1e-8) or (side == 'buy' and delta <= 1e-8):
                            continue
                        if side == 'buy' and iid not in eligible.index:
                            self.rejections.append(dict(date=day, instrument_id=iid, side=side, reason='UNIVERSE_INELIGIBLE'))
                            continue
                        security = eligible.loc[iid].to_dict() if iid in eligible.index else {}
                        self.order(iid, side, abs(delta), prices.loc[iid].to_dict(), day, security)
            held = [iid for iid in self.lots if self.quantity(iid) > 1e-10]
            market_value = sum(self.quantity(iid) * prices.loc[iid, 'raw_close'] for iid in held)
            for iid in held:
                positions.append(dict(date=day, instrument_id=iid, quantity=self.quantity(iid),
                                      cost_basis=self.cost_basis(iid), market_value=self.quantity(iid) * prices.loc[iid, 'raw_close']))
            nav.append(dict(date=day, valuation_at=self.adapter.calendar.at(day, 'close'), nav=(self.cash + market_value + receivable) / self.initial_cash,
                            cash=self.cash, available_cash=self.available_cash(), unsettled_cash=sum(p['amount'] for p in self.pending),
                            dividend_receivable=receivable, currency=self.adapter.currency))
        return BacktestResult(pd.DataFrame(nav), pd.DataFrame(self.trades), pd.DataFrame(positions),
                              pd.DataFrame(self.rejections), pd.DataFrame(self.action_log), 'total_return_cash_dividends_not_reinvested_automatically')


def fast_backtest(bars, targets, calendar, fees, start, end, initial_cash=1_000_000):
    """Friction-aware fractional adjusted-price research, without share actions.

    Comparable prices must declare whether they include dividends. This function
    reports price return; it never fabricates corporate action cash flows.
    """
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ContractError('positive finite initial cash required')
    required = {'date', 'instrument_id', 'adj_open', 'adj_close'}
    if required - set(bars) or bars.duplicated(['date', 'instrument_id']).any():
        raise ContractError('invalid fast backtest bar contract')
    for day, group in targets.groupby('date'):
        if group.instrument_id.duplicated().any() or not np.isfinite(group.target_weight).all() or (group.target_weight < 0).any() or group.target_weight.sum() > 1 + 1e-8:
            raise ContractError('invalid long-only target weights')
        if 'signal_at' in group and (pd.to_datetime(group.signal_at, utc=True) > calendar.at(day, 'close')).any():
            raise ContractError('signal was unavailable at declared signal close')
    groups = {d: f.set_index('instrument_id') for d, f in bars.groupby('date')}
    scheduled = {calendar.shift(d, 1): f.set_index('instrument_id').target_weight.to_dict() for d, f in targets.groupby('date')}
    cash, shares, nav, trades, rejected, positions = float(initial_cash), {}, [], [], [], []
    definition = 'adjusted_price_return_excluding_cash_dividends'

    def require_prices(day, prices, needed):
        issues = []
        for iid in sorted(needed):
            for field in ('adj_open', 'adj_close'):
                value = prices.loc[iid, field] if iid in prices.index else None
                if value is None or not np.isfinite(value) or value <= 0:
                    issues.append(dict(instrument_id=iid, field=field, value=value,
                        quantity=shares.get(iid, 0.), reason='NO_OBSERVATION' if iid not in prices.index else 'NONPOSITIVE_OR_NONFINITE'))
        if issues:
            partial = BacktestResult(pd.DataFrame(nav), pd.DataFrame(trades), pd.DataFrame(positions),
                                    pd.DataFrame(rejected), pd.DataFrame(), definition)
            raise ValuationError('missing fast valuation; no forward fill: ' + str(day.date()) + ' ' +
                ', '.join(sorted({x['instrument_id'] for x in issues})), date=day, issues=issues,
                cash=cash, quantities=shares, partial=partial)

    for day in calendar.sessions(start, end).index:
        p = groups.get(day, pd.DataFrame(columns=['adj_open', 'adj_close']))
        held = [iid for iid, q in shares.items() if q > 1e-10]
        # Validate new purchases' closing marks BEFORE any order is booked too.
        # Otherwise a valid open and missing close could produce a NaN NAV.
        needed = set(held) | set(scheduled.get(day, {}))
        require_prices(day, p, needed)
        if day not in groups:
            raise ContractError('missing fast session prices')
        equity = cash + sum(shares[iid] * p.loc[iid, 'adj_open'] for iid in held)
        if day in scheduled:
            w = scheduled[day]
            desired = {iid: equity * w.get(iid, 0) / p.loc[iid, 'adj_open'] for iid in set(held) | set(w)}
            for side in ('sell', 'buy'):
                for iid in sorted(desired):
                    delta = desired[iid] - shares.get(iid, 0)
                    if (side == 'sell' and delta >= 0) or (side == 'buy' and delta <= 0):
                        continue
                    price, qty = p.loc[iid, 'adj_open'], abs(delta)
                    fee = fees.charge(day, side, qty, price)
                    if side == 'buy':
                        qty = max(0, min(qty, (cash - fee) / price))
                        fee = fees.charge(day, side, qty, price)
                        if qty <= 1e-10 or qty * price + fee > cash + 1e-8:
                            rejected.append(dict(date=day, instrument_id=iid, side=side, reason='INSUFFICIENT_CASH_FOR_FEES'))
                            continue
                        cash -= qty * price + fee
                        cash = max(0.0, cash)
                        shares[iid] = shares.get(iid, 0) + qty
                    else:
                        if cash + qty * price - fee < -1e-8:
                            rejected.append(dict(date=day, instrument_id=iid, side=side, reason='INSUFFICIENT_CASH_FOR_FEES'))
                            continue
                        cash += qty * price - fee
                        cash = max(0.0, cash)
                        shares[iid] -= qty
                    trades.append(dict(date=day, instrument_id=iid, side=side, quantity=qty, price=price, fee=fee))
        close_equity = cash + sum(q * p.loc[iid, 'adj_close'] for iid, q in shares.items() if q > 1e-10)
        for iid, q in shares.items():
            if q > 1e-10:
                positions.append(dict(date=day, instrument_id=iid, quantity=q,
                    price=p.loc[iid, 'adj_close'], market_value=q * p.loc[iid, 'adj_close'],
                    price_basis='adjusted_units_excluding_cash_dividends'))
        nav.append(dict(date=day, nav=close_equity / initial_cash, cash=cash))
    return BacktestResult(pd.DataFrame(nav), pd.DataFrame(trades), pd.DataFrame(positions), pd.DataFrame(rejected), pd.DataFrame(), definition)
