"""Isolated, explicitly conditional research. No formal admission dependencies.

Prices and quantities are synthetic adjusted units. UNKNOWN rights never become
cash, a forced sale, or a certified return. Existing backtest engines are unused.
"""
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


CLASSIFICATION = 'EXPLORATORY_SCENARIO'
GATES = ('PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED', 'NO LIVE MONEY')
SCORE_COLUMNS = {'date', 'instrument_id', 'score', 'signal_at'}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Constraints:
    top_k: int = 20
    frequency: int = 5
    min_notional: float = 0.
    min_weight_delta: float = 0.
    buffer_fraction: float = 0.
    reversal_sessions: int = 0
    min_equity: float = 0.

    def __post_init__(self):
        for name in ('top_k', 'frequency', 'reversal_sessions'):
            x = getattr(self, name)
            if isinstance(x, bool) or not isinstance(x, int) or x < (0 if name == 'reversal_sessions' else 1):
                raise ScenarioError('invalid integer constraint: ' + name)
        for name in ('min_notional', 'min_weight_delta', 'buffer_fraction', 'min_equity'):
            x = getattr(self, name)
            if not math.isfinite(x) or x < 0:
                raise ScenarioError('invalid constraint: ' + name)
        if self.min_weight_delta > 1 or self.buffer_fraction > 1:
            raise ScenarioError('weight/buffer outside [0,1]')


@dataclass(frozen=True)
class Assumptions:
    price_basis: str
    corporate_actions: str
    terminal_rights: str
    calendar: str
    fee_evidence: str
    missing_marks: str = 'UNKNOWN'
    annual_cash_yield: float = 0.
    minimum_commission: float = 5.

    def __post_init__(self):
        for key in ('price_basis', 'corporate_actions', 'terminal_rights', 'calendar', 'fee_evidence'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ScenarioError('explicit assumption required: ' + key)
        if self.missing_marks not in {'UNKNOWN', 'FROZEN_LAST_UNTRADEABLE', 'ZERO_CLAIM_UNTRADEABLE'}:
            raise ScenarioError('unknown missing mark assumption')
        if not 0 <= self.annual_cash_yield <= 1 or not math.isfinite(self.minimum_commission) or self.minimum_commission < 0:
            raise ScenarioError('invalid income/commission assumption')


def cost_parts(day, side, notional, minimum=5.):
    if side not in {'buy', 'sell'} or not math.isfinite(notional) or notional < 0 or not math.isfinite(minimum) or minimum < 0:
        raise ScenarioError('invalid cost input')
    proportional = notional * .0003
    commission = max(minimum, proportional)
    stamp = notional * (.001 if pd.Timestamp(day) < pd.Timestamp('2023-08-28') else .0005) if side == 'sell' else 0.
    slip = notional * .0005
    return dict(commission=commission, proportional_commission=proportional,
                floor_excess=commission-proportional, stamp=stamp, slippage=slip,
                fee=commission+stamp+slip)


@dataclass(frozen=True)
class ScenarioResult:
    """No BacktestResult inheritance, target weights, formal receipt or promotion."""
    metadata: dict
    ledger: pd.DataFrame
    fills: pd.DataFrame
    holdings: pd.DataFrame
    decisions: pd.DataFrame
    uncertainty: pd.DataFrame

    def for_consumer(self, consumer):
        raise ScenarioError('EXPLORATORY_SCENARIO prohibited consumer: ' + str(consumer))

    def save(self, root, run_id):
        root = Path(root)
        if root.name != 'exploratory-scenarios' or root.is_symlink() or root.resolve() != root.absolute():
            raise ScenarioError('dedicated regular exploratory-scenarios directory required')
        if not run_id or Path(run_id).name != run_id or run_id in {'.', '..'}:
            raise ScenarioError('simple scenario run identifier required')
        out = root / run_id
        out.mkdir(parents=True, exist_ok=False)
        for name in ('ledger', 'fills', 'holdings', 'decisions', 'uncertainty'):
            f = getattr(self, name).add_prefix('scenario_')
            f.to_parquet(out / (name + '.parquet'), index=False)
        (out / 'SCENARIO_ONLY.json').write_text(json.dumps(self.metadata, indent=2, allow_nan=False, default=str)+'\n', encoding='utf-8')
        return out


def prepare_scores(scores, calendar):
    """Refuse labels, returns, train/test targets or unknown extra columns."""
    if set(scores) != SCORE_COLUMNS:
        raise ScenarioError('scores require exactly date/instrument_id/score/signal_at; labels forbidden')
    f = scores.copy()
    f['date'] = pd.to_datetime(f.date)
    if f.empty or f.duplicated(['date', 'instrument_id']).any() or not np.isfinite(f.score).all():
        raise ScenarioError('empty/duplicate/nonfinite scores')
    if f.instrument_id.isna().any() or not f.instrument_id.map(lambda x: isinstance(x, str) and bool(x)).all():
        raise ScenarioError('invalid score identities')
    signal = pd.to_datetime(f.signal_at, utc=True)
    cutoff = f.date.map(calendar.schedule.close)
    if cutoff.isna().any() or signal.isna().any() or (signal > cutoff).any():
        raise ScenarioError('signal unavailable at declared close')
    f = f.sort_values(['date', 'score', 'instrument_id'], ascending=[True, False, True], kind='stable')
    f['rank'] = f.groupby('date').cumcount()+1
    return {day: group.set_index('instrument_id')[['rank', 'score']] for day, group in f.groupby('date', sort=True)}


def run_scenario(bars, scores, calendar, constraints, assumptions, *, initial_cash=1e6):
    return run_prepared_scenario(bars, prepare_scores(scores, calendar), calendar, constraints, assumptions, initial_cash=initial_cash)


def run_prepared_scenario(bars, ranked, calendar, constraints, assumptions, *, initial_cash=1e6):
    """Prepared rankings are internal read-only input, never fitted to outcomes.

    Decisions use previous close scores/holdings. Open marks size orders at the
    declared next open. Close prices value completed sessions only. Missing mark
    hypotheses freeze the entire security; no assumed liquidity or fake fill.
    """
    if not isinstance(constraints, Constraints) or not isinstance(assumptions, Assumptions):
        raise ScenarioError('typed constraints and explicit assumptions required')
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ScenarioError('positive finite initial cash required')
    if {'date', 'instrument_id', 'adj_open', 'adj_close'} - set(bars) or bars.duplicated(['date', 'instrument_id']).any():
        raise ScenarioError('invalid scenario bars')
    sessions = calendar.schedule.index
    days = sorted(ranked)
    if not days or any(d not in sessions for d in days):
        raise ScenarioError('invalid ranked calendar')
    start, end = days[0], calendar.shift(days[-1], 1)
    dates = calendar.sessions(start, end).index
    # These ranks must have been prepared by prepare_scores. A frozen mapping is
    # a performance interface; this checks ordering, not historical data trust.
    for d, frame in ranked.items():
        if set(frame) != {'rank', 'score'} or not frame.index.is_unique or not np.isfinite(frame.to_numpy()).all():
            raise ScenarioError('invalid prepared ranks')
        if not np.array_equal(frame['rank'].to_numpy(), np.arange(1, len(frame)+1)):
            raise ScenarioError('ranks must cover full current cross section')
    groups = {d: f.set_index('instrument_id')[['adj_open','adj_close']] for d, f in bars.groupby('date', sort=False)}
    anchor = sessions.get_loc(start)
    signal_days = {d for d in days if (sessions.get_loc(d)-anchor) % constraints.frequency == 0}
    schedules = {calendar.shift(d, 1): d for d in signal_days}
    cash, q, last_marks, last_fills = float(initial_cash), {}, {}, {}
    nav, fills, holds, decisions, unknown = [], [], [], [], []
    previous_equity, status = float(initial_cash), 'CONDITIONAL_COMPLETE'
    for day in dates:
        prices = groups.get(day, pd.DataFrame(columns=['adj_open', 'adj_close']))
        held = {i for i, x in q.items() if x > 1e-10}
        sd = schedules.get(day)
        ranks = ranked.get(sd) if sd is not None else None
        selected = []
        if ranks is not None:
            limit = constraints.top_k + math.ceil(constraints.top_k*constraints.buffer_fraction)
            selected = [i for i in ranks.index if i in held and ranks.loc[i,'rank'] <= limit][:constraints.top_k] if constraints.buffer_fraction else []
            for i in ranks.index:
                if len(selected) >= constraints.top_k:
                    break
                if i not in selected:
                    selected.append(i)
            if len(selected) < constraints.top_k:
                raise ScenarioError('cross section smaller than K')
        needed = held | set(selected)
        marks, untradeable = {}, set()
        failed = False
        for i in sorted(needed):
            values = prices.loc[i].to_numpy(dtype=float) if i in prices.index else np.array([np.nan,np.nan])
            valid_open = np.isfinite(values[0]) and values[0] > 0
            if valid_open:
                marks[i] = (float(values[0]), float(values[1]))
            else:
                hypothesis = assumptions.missing_marks
                prior = last_marks.get(i)
                can_assume = prior is not None and hypothesis != 'UNKNOWN'
                unknown.append(dict(date=day,phase='OPEN',instrument_id=i,quantity=q.get(i,0.),last_observed_mark=prior,
                    policy=hypothesis, trusted_value='UNKNOWN',assumption_applied=can_assume))
                if not can_assume:
                    failed = True
                else:
                    mark = prior if hypothesis == 'FROZEN_LAST_UNTRADEABLE' else 0.
                    marks[i] = (mark,mark)
                    untradeable.add(i)
        if failed:
            status = 'UNKNOWN_VALUATION_STOP'
            break
        opening = cash + sum(q[i]*marks[i][0] for i in held)
        income = sum(q[i]*marks[i][0] for i in held)*assumptions.annual_cash_yield/252
        cash += income
        opening += income
        dayfees, daynotional = 0., 0.
        if ranks is not None:
            weights = {i: 1./constraints.top_k for i in selected}
            desired = {i: opening*weights.get(i,0.)/marks[i][0] if marks[i][0]>0 else q.get(i,0.) for i in needed}
            for side in ('sell','buy'):
                for i in sorted(desired):
                    delta = desired[i]-q.get(i,0.)
                    if (side == 'sell' and delta >= 0) or (side == 'buy' and delta <= 0):
                        continue
                    price, qty = marks[i][0], abs(delta)
                    notional = qty*price
                    weight_delta = notional/opening if opening>0 else 0.
                    previous = last_fills.get(i)
                    reason = None
                    if i in untradeable:
                        reason = 'ASSUMED_MARK_UNTRADEABLE'
                    elif opening < constraints.min_equity:
                        reason = 'BELOW_EXECUTABLE_EQUITY'
                    elif notional < constraints.min_notional:
                        reason = 'MIN_NOTIONAL'
                    elif weight_delta < constraints.min_weight_delta:
                        reason = 'MIN_WEIGHT_DELTA'
                    elif previous and previous[0] != side and sessions.get_loc(day)-previous[1] <= constraints.reversal_sessions:
                        reason = 'REVERSAL_COOLDOWN'
                    cp = cost_parts(day, side, notional, assumptions.minimum_commission)
                    if reason is None and side == 'buy':
                        qty = max(0.,min(qty,(cash-cp['fee'])/price))
                        notional = qty*price
                        cp = cost_parts(day,side,notional,assumptions.minimum_commission)
                        if qty <= 1e-10 or notional+cp['fee'] > cash+1e-8:
                            reason = 'INSUFFICIENT_CASH_FOR_FEES'
                        elif notional < constraints.min_notional:
                            reason = 'MIN_NOTIONAL_AFTER_CASH_CAP'
                        elif (notional/opening if opening>0 else 0.) < constraints.min_weight_delta:
                            reason = 'MIN_WEIGHT_AFTER_CASH_CAP'
                    if reason is None and side == 'sell' and cash+notional-cp['fee'] < -1e-8:
                        reason = 'INSUFFICIENT_CASH_FOR_FEES'
                    rank = int(ranks.loc[i,'rank']) if i in ranks.index else None
                    decisions.append(dict(date=day,signal_date=sd,instrument_id=i,side=side,desired_notional=abs(delta)*price,
                        executed_notional=notional if reason is None else 0.,rank=rank,target_weight=weights.get(i,0.),
                        weight_delta=weight_delta,reason=reason or 'FILLED'))
                    if reason:
                        continue
                    cash += (-notional if side == 'buy' else notional)-cp['fee']
                    cash = max(0.,cash)
                    q[i] = q.get(i,0.) + (qty if side == 'buy' else -qty)
                    last_fills[i] = (side,sessions.get_loc(day))
                    fills.append(dict(date=day,signal_date=sd,instrument_id=i,side=side,quantity=qty,price=price,
                        notional=notional,rank=rank,cash_after=cash,**cp))
                    dayfees += cp['fee']
                    daynotional += notional
        # A missing CLOSE must never suppress an otherwise executable OPEN.
        # Record any fills first, then declare the end-of-day mark unknown.
        for i,x in q.items():
            if x <= 1e-10 or (np.isfinite(marks[i][1]) and marks[i][1] > 0) or i in untradeable:
                continue
            prior = last_marks.get(i)
            hypothesis = assumptions.missing_marks
            can_assume = prior is not None and hypothesis != 'UNKNOWN'
            unknown.append(dict(date=day,phase='CLOSE',instrument_id=i,quantity=x,last_observed_mark=prior,
                policy=hypothesis,trusted_value='UNKNOWN',assumption_applied=can_assume))
            if not can_assume:
                failed = True
            else:
                marks[i] = (marks[i][0], prior if hypothesis == 'FROZEN_LAST_UNTRADEABLE' else 0.)
                untradeable.add(i)
        if failed:
            status = 'UNKNOWN_VALUATION_STOP'
            break
        close = cash + sum(x*marks[i][1] for i,x in q.items() if x>1e-10)
        latest_signal = sessions[sessions.get_loc(day)-1] if sessions.get_loc(day)>0 else None
        latest = ranked.get(latest_signal)
        for i,x in q.items():
            if x>1e-10:
                holds.append(dict(date=day,instrument_id=i,quantity=x,mark=marks[i][1],market_value=x*marks[i][1],
                    rank_previous_close=int(latest.loc[i,'rank']) if latest is not None and i in latest.index else None,
                    assumed_mark=i in untradeable))
        for i in needed-untradeable:
            last_marks[i] = marks[i][1]
        nav.append(dict(date=day,beginning_equity=previous_equity,opening_equity=opening,conditional_equity=close,cash=cash,
            fee=dayfees,notional=daynotional,gross_turnover=daynotional/previous_equity if previous_equity>0 else None,
            artificial_income=income,rebalance=sd is not None,trusted_equity='UNKNOWN'))
        previous_equity = close
    metadata = dict(schema='EXPLORATORY_SCENARIO_RESULT_V1',classification=CLASSIFICATION,status=status,
        assumptions=asdict(assumptions),constraints=asdict(constraints),initial_cash=initial_cash,
        trusted_return='UNKNOWN',trusted_equity_range='UNKNOWN',gates=list(GATES),formal_admission=False,
        execution_admission=False,prohibited_consumers=['LIVE','FORMAL_FORWARD','FINANCIAL_ADMISSION','EXECUTION_ADMISSION'],
        return_definition='CONDITIONAL_ADJUSTED_PRICE_PLUS_EXPLICIT_ARTIFICIAL_INCOME_NOT_CERTIFIED_TOTAL_RETURN')
    return ScenarioResult(metadata,*[pd.DataFrame(x) for x in (nav,fills,holds,decisions,unknown)])
