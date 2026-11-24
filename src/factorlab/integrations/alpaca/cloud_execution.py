"""Natural-week original V69 preparation over the shared E03 execution journal."""
from datetime import timedelta
from decimal import Decimal as D

import pandas as pd

from factorlab.strategies.v69 import ASSETS, V69Strategy
from factorlab.strategies.v69_alpaca import create_alpaca_plan, to_adjusted_holdings
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.portfolio_execution import InstrumentRules, PortfolioStore
from .adapter import OrderNotFound
from .http import AlpacaError
from .market_data.cache import MarketCache
from .market_data.inputs import Instrument
from .portfolio import AlpacaPortfolioRunner, portfolio_quote


class PreparationIncomplete(RuntimeError):
    """The market supplied no complete execution-basis preparation yet."""


def execution_factors(prepared, announcements, session):
    """Use unchanged overnight share bases; action days require explicit rebasing."""
    relevant = [row for row in announcements if row['ex_date'] == session and
                {row['initiating_symbol'], row['target_symbol']} & set(ASSETS)]
    if relevant:
        # TODO: reconcile split/dividend/spinoff execution-day units before enabling action-day plans.
        raise PreparationIncomplete('EXECUTION_DAY_CORPORATE_ACTION')
    factors = {}
    for symbol in ASSETS:
        bars = prepared.bars.loc[prepared.bars.instrument_id == symbol].sort_values('session')
        last = bars.iloc[-1]
        factor = D(str(last.adjusted_close)) / D(str(last.raw_close))
        if not factor.is_finite() or factor <= 0:
            raise PreparationIncomplete('INVALID_ADJUSTMENT_FACTOR:' + symbol)
        factors[symbol] = factor
    return factors


class WeeklyExecution:
    def __init__(self, root, adapter, *, clock):
        self.root, self.adapter, self.clock = root, adapter, clock
        self.store = PortfolioStore(root / 'portfolio.sqlite3')
        self.runner = AlpacaPortfolioRunner(adapter, self.store, order_not_found=OrderNotFound,
                                            rejection_error=AlpacaError, clock=clock)
        self.state = {'state': 'STARTING', 'name': 'v69-original', 'frequency': 'natural-week'}
        self.execution = {'enabled': True, 'state': 'RECOVERING'}
        self.identity = None

    def last_plan(self, account):
        row = self.store.db.execute(
            'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY session DESC,rowid DESC LIMIT 1',
            (account, 'V69_CAUSAL')).fetchone()
        return self.store.plan(row[0]) if row else None

    def handover(self, account):
        symbols = tuple(symbol for symbol, in self.store.db.execute(
            'SELECT symbol FROM portfolio_claims WHERE account=? AND strategy=?',
            (account, 'ALP-E03 TEST')))
        if symbols:
            self.runner.executor.transfer_flat_ownership(account_id=account,
                from_strategy='ALP-E03 TEST', to_strategy='V69_CAUSAL', symbols=symbols)

    def prepare(self, service, window, at, facts):
        session, cutoff = window['execution_session'], window['signal_cutoff']
        sessions = service.calendar.schedule.index
        history_days = sessions[sessions <= pd.Timestamp(cutoff)][-252:]
        if len(history_days) != 252:
            raise PreparationIncomplete('INSUFFICIENT_CALENDAR_HISTORY')
        instruments = tuple(Instrument(s, s, at) for s in ASSETS)
        service.cache = MarketCache(self.root / 'history')
        prepared = service.prepare_history(instruments, start=history_days[0], end=cutoff,
                                          adjustment_basis='yahoo_ratio')
        if (prepared.adjusted_close.shape != (252, len(ASSETS)) or
                prepared.adjusted_close.isna().any().any() or
                prepared.adjusted_volume.isna().any().any()):
            raise PreparationIncomplete('INCOMPLETE_PRIOR_SESSION_MATRICES')
        announcements = self.adapter.http.request('GET', '/v2/corporate_actions/announcements',
            params={'ca_types': 'dividend,merger,spinoff,split', 'since': cutoff,
                    'until': session, 'date_type': 'ex_date'})
        # This API rejects a saturated result rather than proving absence of an action.
        if len(announcements) >= 1000:
            raise PreparationIncomplete('SATURATED_CORPORATE_ACTION_WINDOW')
        factors = execution_factors(prepared, announcements, session)
        snapshot = self.adapter.recover()
        if not snapshot.reconciled:
            raise PreparationIncomplete('ACCOUNT_NOT_RECONCILED')
        facts = self.adapter.read_account_details()
        holdings = {p.instrument_id: p.quantity for p in snapshot.positions if p.instrument_id in ASSETS}
        if any(p.quantity for p in snapshot.positions if p.instrument_id not in ASSETS):
            raise PreparationIncomplete('INVENTORY_OUTSIDE_ORIGINAL_UNIVERSE')
        rules = self.rules()
        last = self.last_plan(facts.account_id)
        checkpoint = {} if last is None else last['request']['signal_inputs']['checkpoint']
        decision = V69Strategy().decide(dict(session=session, calendar=service.calendar,
            close=prepared.adjusted_close, volume=prepared.adjusted_volume,
            holdings=to_adjusted_holdings(holdings, factors), cash=float(facts.cash)), checkpoint)
        directory = self.root / 'signals'
        directory.mkdir(mode=0o700, exist_ok=True)
        # Retain the exact acquisition vintages; the E03 task is the commit point.
        destination = directory / (session + '.parquet')
        temporary = destination.with_suffix('.tmp')
        prepared.bars.to_parquet(temporary, index=False)
        temporary.replace(destination)
        return create_alpaca_plan(self.runner.executor, decision=decision,
            account_id=facts.account_id, session=session, now=self.clock(), raw_holdings=holdings,
            factors=factors, quantity_steps={s: r.quantity_step for s, r in rules.items()},
            initial_inventory={p.instrument_id: (p.quantity, p.entry_price)
                               for p in snapshot.positions if p.instrument_id in ASSETS},
            signal_provenance={'source': 'ALPACA:iex', 'signal_cutoff': cutoff,
                'factor_session': session, 'adjustment_basis': prepared.adjustment_basis,
                'prepared_at': prepared.prepared_at.isoformat(), 'snapshot': destination.name,
                'action_ids': [row['id'] for row in announcements]})

    def rules(self):
        result = {}
        for symbol in ASSETS:
            asset = self.adapter.read_asset(symbol)
            result[symbol] = InstrumentRules(D('.000000001') if asset['fractionable'] else D(1),
                D(1), tradable=asset['tradable'], time_in_force='DAY',
                price_tick=D('.01'), limit_price_tolerance=D('.001'))
        return result

    def tick(self, service, window, at, facts):
        self.handover(facts.account_id)
        plan = self.last_plan(facts.account_id)
        if plan is not None:
            self.identity = plan['id']
            if plan['phase'] != 'COMPLETE' and not (
                    window['due'] and plan['request']['session'] == window['execution_session']):
                self.runner.report(plan['id'])
                orders = self.store.orders(plan['id'])
                if any(o['uncertain'] or o['record'].snapshot.status not in TERMINAL for o in orders):
                    self.state['state'] = 'PREVIOUS_TASK_RECONCILIATION_REQUIRED'
                    self.execution.update(state='RECONCILING', task=plan['id'])
                    return
                self.runner.executor.abandon(plan['id'])
                plan = self.store.plan(plan['id'])
        if not window['due']:
            self.state.update(state='WAITING_NEXT_WEEK', reason=None)
            self.execution.update(state='IDLE', task=self.identity)
            return
        if plan is None or plan['request']['session'] != window['execution_session']:
            try:
                self.identity = self.prepare(service, window, at, facts)
            except PreparationIncomplete as error:
                self.state.update(state='PREPARATION_INCOMPLETE', reason=str(error))
                self.execution.update(state='BLOCKED_INPUTS')
                return
            plan = self.store.plan(self.identity)
        if plan['phase'] == 'COMPLETE':
            self.state.update(state='WEEK_COMPLETE', reason=None)
            self.execution.update(state='COMPLETE', task=plan['id'])
            return
        decision_at = self.clock()
        instruments = tuple(Instrument(s, s, decision_at) for s in ASSETS)
        opened = service.calendar.at(window['execution_session'], 'open').to_pydatetime()
        closed = service.calendar.at(window['execution_session'], 'close').to_pydatetime()
        def quotes():
            observed = service.refresh_quotes(instruments, decision_at=decision_at,
                decision_window=(opened, closed), max_age=timedelta(seconds=5))
            return {s: portfolio_quote(observed[s], instrument_id=s, symbol=s)
                    for s in ASSETS if observed[s] is not None}
        result = self.runner.run_once(plan['id'], quotes=quotes, rules=self.rules())
        self.state.update(state='RUNNING' if result.phase != 'COMPLETE' else 'WEEK_COMPLETE', reason=None)
        self.execution.update(state=result.phase, task=plan['id'], reasons=dict(result.reasons))

    def close(self):
        self.store.close()
