"""Instance event ownership and recovery over the shared Alpaca account runner."""
from hashlib import sha256
import json
import os
from pathlib import Path

from factorlab.errors import DataQualityError
from factorlab.strategies.events import StrategyInputUnavailable, validate_event
from factorlab.strategies.loader import StrategyWorkerError
from factorlab.strategies.model import ModelNotEffective
from factorlab.trading.storage import decode, dumps
from factorlab.trading.portfolio_execution import TERMINAL
from .cloud_execution import PreparationIncomplete
from .market_data.client import MarketDataError
from .scheduling import schedule_window


class AlpacaInstance:
    def __init__(self, root, binding, package_id, spec, runtime, runner, data, *, clock):
        self.root = Path(root)
        self.binding, self.package_id, self.spec = binding, package_id, spec
        self.runtime, self.runner, self.data, self.clock = runtime, runner, data, clock
        self.state = {'state': 'RECOVERING', 'instance_id': binding['instance_id']}

    def plans(self):
        rows = self.runner.store.db.execute(
            'SELECT id FROM portfolio_plans WHERE account=? AND strategy=? ORDER BY rowid',
            (self.binding['account_id'], self.binding['instance_id'])).fetchall()
        return [self.runner.store.plan(identity) for identity, in rows]

    def allocation(self, account):
        owner, store = self.binding['instance_id'], self.runner.store
        funding = store.allocation(self.binding['account_id'], owner)
        if funding is None or funding['allocation_id'] != self.binding['allocation_id']:
            raise ValueError('registered instance allocation required')
        return dict(allocation_id=funding['allocation_id'], currency='USD',
                    capital=funding['capital'], cash=store.cash(account.account_id, owner),
                    positions=store.holdings(account.account_id, owner))

    def event(self, service, window, account):
        binding_key = [[field, self.binding[field]] for field in sorted(self.binding)]
        key = sha256(dumps([binding_key, self.package_id, window.event_id]).encode()).hexdigest()
        directory = self.root / 'events'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / (key + '.json')
        if destination.exists():
            return decode(json.loads(destination.read_text(encoding='utf-8')))
        orders = self.orders() if 'order_event' in self.spec.data['data_types'] else ()
        event = self.data.event(service, self.spec, self.binding, self.allocation(account), window,
                               owned_orders=orders)
        try:
            validate_event(event, self.spec)
        except ValueError as error:
            raise StrategyInputUnavailable(str(error)) from error
        if 'model' in self.runtime.strategy.binding:
            self.runtime.strategy.require_event_time(event['event_time'])
        temporary = destination.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            output.write(dumps(event) + '\n')
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
        return event

    def orders(self):
        result = []
        for plan in self.plans():
            for child in self.runner.store.orders(plan['id']):
                snapshot = child['record'].snapshot
                result.append(dict(symbol=snapshot.intent.instrument_id,
                    client_order_id=snapshot.intent.client_order_id, status=snapshot.status.value,
                    filled_quantity=snapshot.cumulative_filled_quantity, uncertain=child['uncertain']))
        return result

    def recover(self, service, session):
        plans = self.plans()
        if plans:
            self.runner.report(plans[-1]['id'])
        for plan in self.plans():
            if plan['phase'] in {'COMPLETE', 'ABANDONED'}:
                continue
            if session is None:
                return False
            if plan['request']['session'] != str(session.date()):
                self.runner.report(plan['id'])
                for child in self.runner.store.orders(plan['id']):
                    if child['uncertain'] or child['record'].snapshot.status not in TERMINAL:
                        self.runner.executor.cancel(plan['id'],
                            child['record'].snapshot.intent.client_order_id)
                if any(child['uncertain'] or child['record'].snapshot.status not in TERMINAL
                       for child in self.runner.store.orders(plan['id'])):
                    return False
                self.runner.executor.abandon(plan['id'])
                continue
            symbols = plan['request']['owned']
            quotes = self.data.quotes(service, symbols, session=session, decision_at=self.clock())
            rules = self.data.assets.rules(symbols,
                prices={symbol: quote.ask for symbol, quote in quotes.items() if quote.valid},
                fractional_quantities=self.spec.execution['fractional_quantities'])
            self.runner.run_once(plan['id'], quotes=lambda: self.data.quotes(service, symbols,
                session=session, decision_at=self.clock()), rules=rules)
            if self.runner.store.plan(plan['id'])['phase'] not in {'COMPLETE', 'ABANDONED'}:
                return False
        self.runtime.recover()
        return True

    def tick(self, service, at, account):
        session = service.calendar.regular_session(at)
        if session is None:
            self.recover(service, session)
            self.state.update(state='WAITING_SESSION')
            return
        if not self.recover(service, session):
            self.state.update(state='PREVIOUS_TASK_RECONCILIATION_REQUIRED')
            return
        window = schedule_window(service.calendar, self.spec.schedule, at)
        if window is None:
            self.state.update(state='WAITING_EVENT')
            return
        try:
            event = self.event(service, window, account)
        except (PreparationIncomplete, StrategyInputUnavailable, DataQualityError, MarketDataError) as error:
            self.state.update(state='PREPARATION_INCOMPLETE', reason=str(error))
            return
        except ModelNotEffective as error:
            self.state.update(state='WAITING_MODEL_EFFECTIVE', reason=str(error))
            return
        try:
            self.runtime.decide(event)
        except (StrategyInputUnavailable, ModelNotEffective) as error:
            self.state.update(state='PREPARATION_INCOMPLETE', reason=str(error))
            return
        except StrategyWorkerError as error:
            self.state.update(state='STRATEGY_FAILED', reason=str(error))
            return
        self.recover(service, session)
        self.state.update(state='RUNNING', event_id=window.event_id, reason=None)
