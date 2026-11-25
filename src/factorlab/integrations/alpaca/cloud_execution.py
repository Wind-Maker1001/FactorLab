"""Natural-week original V69 preparation over the shared E03 execution journal."""
from datetime import timedelta
from decimal import Decimal as D
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from factorlab.strategies.v69 import ASSETS, V69Strategy
from factorlab.strategies.v69_alpaca import create_alpaca_plan, to_adjusted_holdings
from factorlab.trading.contracts import TERMINAL
from factorlab.trading.storage import decode, dumps
from factorlab.trading.portfolio_execution import InstrumentRules, PortfolioStore
from .adapter import OrderNotFound
from .corporate_actions import CorporateReceiptJournal
from .http import AlpacaError
from .market_data.cache import MarketCache
from .market_data.inputs import Instrument
from .portfolio import AlpacaPortfolioRunner, portfolio_quote


class PreparationIncomplete(RuntimeError):
    """The market supplied no complete execution-basis preparation yet."""


def execution_factors(prepared, announcements, session, *, execution_bars=None):
    """Use official same-vintage raw/adjusted pairs, never guessed action ratios.

    The previous session ratio is not the execution-session ratio on action days.
    A same-session adjusted/raw pair supplies the composed factor, including
    multiple actions, without applying their ratios twice to vendor adjustments.
    """
    affected = set().union(*(
        {row['initiating_symbol'], row['target_symbol']} & set(ASSETS)
        for row in announcements if row['ex_date'] == session))
    factors = {}
    for symbol in ASSETS:
        if symbol in affected:
            if execution_bars is None:
                raise PreparationIncomplete('EXECUTION_DAY_CORPORATE_ACTION:' + symbol)
            bars = execution_bars.loc[execution_bars.instrument_id == symbol]
            if bars.empty:
                raise PreparationIncomplete('MISSING_EXECUTION_BASIS:' + symbol)
            last = bars.sort_values('event_at').iloc[-1]
            if (str(pd.Timestamp(last.session).date()) != session or
                    not last.complete or not last.adjusted_bar_complete):
                raise PreparationIncomplete('INCOMPLETE_EXECUTION_BASIS:' + symbol)
        else:
            bars = prepared.bars.loc[prepared.bars.instrument_id == symbol].sort_values('session')
            last = bars.iloc[-1]
        factor = D(str(last.adjusted_close)) / D(str(last.raw_close))
        if not factor.is_finite() or factor <= 0:
            raise PreparationIncomplete('INVALID_ADJUSTMENT_FACTOR:' + symbol)
        factors[symbol] = factor
    return factors


class WeeklyExecution:
    def __init__(self, root, adapter, *, clock, runner=None):
        self.root, self.adapter, self.clock = root, adapter, clock
        self.owns_store = runner is None
        self.store = PortfolioStore(root / 'portfolio.sqlite3') if runner is None else runner.store
        self.runner = (AlpacaPortfolioRunner(adapter, self.store, order_not_found=OrderNotFound,
                                            rejection_error=AlpacaError, clock=clock)
                       if runner is None else runner)
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

    def reconcile_corporate_actions(self, account, cutoff, session):
        first = self.store.db.execute(
            'SELECT MIN(session) FROM portfolio_plans WHERE account=? AND strategy=?',
            (account, 'V69_CAUSAL')).fetchone()[0]
        since = min(first, cutoff) if first else cutoff
        announcements = self.adapter.http.request('GET', '/v2/corporate_actions/announcements',
            params={'ca_types': 'dividend,merger,spinoff,split', 'since': since,
                    'until': session, 'date_type': 'ex_date'})
        if len(announcements) >= 1000:
            raise PreparationIncomplete('SATURATED_CORPORATE_ACTION_WINDOW')
        after = since + 'T00:00:00Z'
        receipts = self.adapter.read_corporate_activities(after=after)
        journal = CorporateReceiptJournal(self.store, account)
        if self.store.allocation(account, 'V69_CAUSAL') is None:
            blocked = journal.reconcile(self.runner.executor, strategy_id='V69_CAUSAL',
                                        announcements=announcements, receipts=receipts)
        else:
            blocked = journal.reconcile_account(self.runner.executor,
                                                announcements=announcements, receipts=receipts)
        affected_blocks = {symbol: reason for symbol, reason in blocked.items() if symbol in ASSETS}
        if affected_blocks:
            raise PreparationIncomplete('CORPORATE_RECEIPTS:' + json.dumps(affected_blocks, sort_keys=True))
        return announcements, receipts, after

    def decide(self, context, checkpoint, *, bridge_inputs):
        return V69Strategy().decide(context, checkpoint)

    def resume_prepared(self, inputs):
        return create_alpaca_plan(self.runner.executor, now=self.clock(), **inputs)

    def prepare(self, service, window, at, facts, *, corporate_evidence=None):
        session, cutoff = window['execution_session'], window['signal_cutoff']
        directory = self.root / 'signals'
        directory.mkdir(mode=0o700, exist_ok=True)
        frozen = directory / (session + '.json')
        if frozen.exists():
            inputs = decode(json.loads(frozen.read_text(encoding='utf-8')))
            if inputs['account_id'] != facts.account_id:
                raise ValueError('frozen preparation belongs to another account')
            snapshot = self.adapter.recover()
            if not snapshot.reconciled:
                raise PreparationIncomplete('ACCOUNT_NOT_RECONCILED')
            inventory = (self.runner.executor.projection(snapshot, 'V69_CAUSAL')
                         if self.store.allocation(facts.account_id, 'V69_CAUSAL') is not None else snapshot)
            current = {p.instrument_id: p.quantity for p in inventory.positions
                       if p.instrument_id in ASSETS and p.quantity}
            original = {symbol: quantity for symbol, quantity in inputs['raw_holdings'].items() if quantity}
            if current != original:
                raise PreparationIncomplete('FROZEN_PREPARATION_HOLDINGS_CHANGED')
            return self.resume_prepared(inputs)
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
        announcements, receipts, receipts_after = (corporate_evidence if corporate_evidence is not None
            else self.reconcile_corporate_actions(facts.account_id, cutoff, session))
        affected = set().union(*(
            {row['initiating_symbol'], row['target_symbol']} & set(ASSETS)
            for row in announcements if row['ex_date'] == session))
        execution_frames = []
        for instrument in instruments:
            if instrument.symbol not in affected:
                continue
            raw = service.history(instrument, session, session, timeframe='1Min', adjustment='raw')
            adjusted = service.history(instrument, session, session, timeframe='1Min', adjustment='all')
            columns = ['instrument_id', 'event_at', 'adjusted_close', 'complete',
                       'received_at', 'closed_at']
            adjusted = adjusted[columns].rename(columns={name: 'adjusted_bar_' + name
                for name in ('complete', 'received_at', 'closed_at')})
            paired = raw.merge(adjusted, on=['instrument_id', 'event_at'],
                               how='inner', validate='one_to_one')
            acquired = pd.Timestamp(self.clock())
            paired = paired.loc[paired.complete & paired.adjusted_bar_complete &
                (paired.received_at <= acquired) & (paired.closed_at <= acquired) &
                (paired.adjusted_bar_received_at <= acquired) &
                (paired.adjusted_bar_closed_at <= acquired)]
            execution_frames.append(paired)
        execution_bars = pd.concat(execution_frames, ignore_index=True) if execution_frames else None
        factors = execution_factors(prepared, announcements, session, execution_bars=execution_bars)
        snapshot = self.adapter.recover()
        if not snapshot.reconciled:
            raise PreparationIncomplete('ACCOUNT_NOT_RECONCILED')
        facts = self.adapter.read_account_details()
        allocation = self.store.allocation(facts.account_id, 'V69_CAUSAL')
        inventory = (self.runner.executor.projection(snapshot, 'V69_CAUSAL')
                     if allocation is not None else snapshot)
        cash = (self.store.cash(facts.account_id, 'V69_CAUSAL', reserve=False)
                if allocation is not None else facts.cash)
        holdings = {p.instrument_id: p.quantity for p in inventory.positions if p.instrument_id in ASSETS}
        retained = {symbol: D(quantity) for symbol, quantity in self.store.db.execute(
            'SELECT symbol,quantity FROM portfolio_claims WHERE account=? AND strategy=?',
            (facts.account_id, 'V69_CAUSAL')) if symbol not in ASSETS}
        external = {p.instrument_id: p.quantity for p in inventory.positions
                    if p.instrument_id not in ASSETS and p.quantity}
        if any(retained.get(symbol, D(0)) != external.get(symbol, D(0))
               for symbol in retained.keys() | external.keys()):
            raise PreparationIncomplete('UNRECONCILED_EXTERNAL_CORPORATE_RIGHTS')
        rules = self.rules()
        last = self.last_plan(facts.account_id)
        checkpoint = {} if last is None else last['request']['signal_inputs']['checkpoint']
        # Publish the preparation before E03 create; a crash in between reuses
        # its original holdings and decision, never a new data acquisition.
        destination = directory / (session + '-' + uuid4().hex + '.parquet')
        prepared.bars.to_parquet(destination, index=False)
        execution_snapshot = None
        if execution_bars is not None:
            execution_snapshot = destination.with_suffix('.execution.parquet')
            execution_bars.to_parquet(execution_snapshot, index=False)
        inputs = dict(account_id=facts.account_id, session=session,
            raw_holdings=holdings, factors=factors,
            quantity_steps={s: r.quantity_step for s, r in rules.items()},
            initial_inventory={p.instrument_id: (p.quantity, p.entry_price)
                               for p in inventory.positions if p.instrument_id in ASSETS},
            signal_provenance={'source': 'ALPACA:iex', 'signal_cutoff': cutoff,
                'factor_session': session, 'adjustment_basis': prepared.adjustment_basis,
                'prepared_at': prepared.prepared_at.isoformat(), 'snapshot': destination.name,
                'execution_basis_snapshot': execution_snapshot.name if execution_snapshot else None,
                'execution_basis_acquired_at': self.clock().isoformat(),
                'announcements': announcements,
                'corporate_receipt_ids': [row['id'] for row in receipts],
                'corporate_receipts_after': receipts_after,
                'action_ids': [row['id'] for row in announcements]})
        inputs['decision'] = self.decide(dict(session=session, calendar=service.calendar,
            close=prepared.adjusted_close, volume=prepared.adjusted_volume,
            holdings=to_adjusted_holdings(holdings, factors), cash=float(cash)), checkpoint,
            bridge_inputs=inputs)
        temporary = frozen.with_suffix('.tmp')
        temporary.write_text(dumps(inputs) + '\n', encoding='utf-8')
        temporary.replace(frozen)
        return create_alpaca_plan(self.runner.executor, now=self.clock(), **inputs)

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
        try:
            corporate_evidence = self.reconcile_corporate_actions(facts.account_id,
                window['signal_cutoff'], at.astimezone(ZoneInfo('America/New_York')).date().isoformat())
        except PreparationIncomplete as error:
            self.state.update(state='PREPARATION_INCOMPLETE', reason=str(error))
            self.execution.update(state='BLOCKED_INPUTS')
            return
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
                self.identity = self.prepare(service, window, at, facts,
                                             corporate_evidence=corporate_evidence)
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
        if self.owns_store:
            self.store.close()
