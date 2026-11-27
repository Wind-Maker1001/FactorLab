"""Portfolio task consumer hosted exclusively by the existing Execute supervisor.

Static ownership: Grid=NVDAUSDT; acceptance portfolio=QQQUSDT,SPYUSDT.
Uses the owner's existing controller, adapter journals and runtime dispatch table.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import os

from factorlab.time_utils import utc_instant
from factorlab.trading.contracts import Side
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.portfolio_runtime import PortfolioRuntime
from factorlab.trading.realtime import identity
from factorlab.trading.storage import dumps
from .demo_cloud_storage import atomic_json
from .demo_runtime import durable_control_state
from .demo_session import demo_account_configuration
from .network import DemoExecutionPlan, DemoNetworkAuthorization
from .protocol import SafetyBlocked

SYMBOLS = frozenset({'QQQUSDT', 'SPYUSDT'})
BUDGET = D(20)
ACCOUNT_BUDGET = D(27)  # 7 USDT retained for the original Grid allocation.


class DemoPortfolioConsumer:
    def __init__(self, session, runtime, directory):
        self.session, self.runtime = session, runtime
        self.directory = Path(directory) / 'portfolio-inbox'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.result = self.directory.parent / 'portfolio-result.json'
        self.mailbox_path = Path(os.environ.get('FACTORLAB_DEMO_TASK_DB',
            str(Path(directory) / 'tradfi-tasks.sqlite3')))
        self.active_mailbox_task = None

    def poll(self):
        try:
            self._poll()
        except Exception as error:
            atomic_json(self.result,dict(status='PORTFOLIO_BLOCKED',reason=str(error)[:300]))
            if self.active_mailbox_task is not None:
                self.receipt('RECONCILING',dict(reason=str(error)[:300]))

    def _poll(self):
        self.active_mailbox_task = None
        if durable_control_state(self.runtime) is not None:
            return
        requests = [p for p in sorted(self.directory.glob('*.json'))
            if self.session.controller.store.event_payload('portfolio-result:' + identity(
                json.loads(p.read_text())['task_id'])) is None]
        if requests:
            request = json.loads(requests[0].read_text())
        else:
            from .tradfi_tasks import TaskMailbox, PortfolioTask
            if not self.mailbox_path.exists():
                return
            mailbox = TaskMailbox(self.mailbox_path)
            try:
                outstanding = mailbox.outstanding()
            finally:
                mailbox.close()
            if not outstanding:
                return
            task_payload = outstanding[0]['task']
            PortfolioTask(task_id=task_payload['task_id'],allocation_id=task_payload['allocation_id'],
                session=task_payload['session'],observed_at=task_payload['observed_at'],
                valid_until=task_payload['valid_until'],equity_usdt=D(task_payload['equity_usdt']),
                weights={k:D(v) for k,v in task_payload['weights'].items()},
                task_type=task_payload['task_type']).payload()
            self.active_mailbox_task = task_payload
            from .tradfi_tasks import TEST_ALLOCATION
            if task_payload['allocation_id'] != TEST_ALLOCATION:
                if self.session.instance_owner is None:
                    raise SafetyBlocked('dynamic task requires the shared instance execution owner')
                status, detail = self.session.instance_owner.accept_task(task_payload)
                self.receipt(status, detail)
                return
            from factorlab.trading.contracts import TERMINAL
            from factorlab.trading.storage import decode
            store = self.session.controller.store
            done = 'portfolio-result:' + identity(task_payload['task_id'])
            saved_result = store.event_payload(done)
            if saved_result is not None:
                saved = decode(json.loads(saved_result))
                self.receipt('COMPLETED' if saved['outcome'] == 'NO_EXECUTABLE_DELTA' else 'REJECTED',
                    dict(outcome=saved['outcome'],journal=done))
                return
            saved_target = store.event_payload('portfolio:' + identity((
                task_payload['allocation_id'],task_payload['task_id'])))
            started = saved_target is not None
            if utc_instant(task_payload['valid_until']) <= utc_instant(self.session._now()):
                recovered = self.session.gateway.recover(self.session.controller)
                if recovered.reconciled and started:
                    portfolio = PortfolioRuntime(self.session.controller,
                        allocation_id=task_payload['allocation_id'], owned_symbols=SYMBOLS,
                        recover=lambda:self.session.gateway.recover(self.session.controller))
                    for order in portfolio.batch_status(task_payload['task_id']):
                        record = self.session.gateway.state.orders.get(order['client_order_id'])
                        if record is not None and record.snapshot.status not in TERMINAL:
                            with self.scope(task_payload['task_id'], order['symbol'], client_id=order['client_order_id']):
                                self.session.cancel(order['client_order_id'])
                    recovered = self.session.gateway.recover(self.session.controller)
                batch_ids = {order['client_order_id'] for order in
                    portfolio.batch_status(task_payload['task_id'])} if started and recovered.reconciled else set()
                if recovered.reconciled and not any(r.snapshot.status not in TERMINAL
                        for k,r in self.session.gateway.state.orders.items() if k in batch_ids):
                    self.receipt('REJECTED',dict(reason='EXPIRED',recovered_inflight=started))
                else:
                    self.receipt('RECONCILING',dict(reason='EXPIRED_INFLIGHT'))
                return
            if started:
                budget = decode(json.loads(saved_target))['equity']
            else:
                funds = self.session.adapter.read_facts().usdt()
                budget = max(D(0),min(D(task_payload['equity_usdt']),BUDGET,
                    funds['walletBalance']+funds['unrealizedProfit']-D(7)))
            request = dict(task_id=task_payload['task_id'],budget_usdt=str(budget),
                           weights=task_payload['weights'])
        task = request['task_id']
        weights = {k:D(v) for k,v in request['weights'].items()}
        budget = D(request['budget_usdt'])
        if not task or len(task) > 100 or set(weights)-SYMBOLS or not D(0) < budget <= BUDGET:
            raise SafetyBlocked('invalid static portfolio request')
        if any(not w.is_finite() or w < 0 for w in weights.values()) or sum(weights.values(),D(0)) > 1:
            raise SafetyBlocked('invalid portfolio weights')
        store = self.session.controller.store
        done = 'portfolio-result:' + identity(task)
        if store.event_payload(done) is not None:
            if self.active_mailbox_task is not None:
                from factorlab.trading.storage import decode
                saved = decode(json.loads(store.event_payload(done)))
                self.receipt('COMPLETED' if saved['outcome'] == 'NO_EXECUTABLE_DELTA' else 'REJECTED',
                    dict(outcome=saved['outcome'],journal=done))
            return
        portfolio = PortfolioRuntime(self.session.controller, allocation_id=(self.active_mailbox_task['allocation_id']
            if self.active_mailbox_task is not None else 'tradfi-demo-acceptance'),
            owned_symbols=SYMBOLS, recover=lambda:self.session.gateway.recover(self.session.controller))
        portfolio.begin(task, weights=weights, equity=budget)
        from .demo_allocations import DemoAllocations
        owners = DemoAllocations(self.session.controller)
        instance_id = 'legacy-portfolio:' + portfolio.allocation_id
        owners.adopt_portfolio_history(instance_id=instance_id, allocation_id=portfolio.allocation_id)
        self.session.controller.limits = replace(self.session.controller.limits,
            max_order_notional=BUDGET, max_instrument_exposure=BUDGET, account_budget=ACCOUNT_BUDGET)
        if self.active_mailbox_task is not None and outstanding[0]['status'] == 'QUEUED':
            self.receipt('ACCEPTED',dict(owner_budget_usdt=str(budget)))
        self.session.shared_account = True
        store.record_metadata('demo:static-shared-account:v1',
            dict(grid=['NVDAUSDT'], portfolio=sorted(SYMBOLS), grid_budget=D(7), portfolio_budget=BUDGET))
        self.session.controller._atomic(lambda: self.session.gateway.state.instruments.update(
            {s:Instrument(s,'USDT') for s in SYMBOLS}))
        account = self.session.gateway.recover(self.session.controller)
        if not account.reconciled:
            return
        for symbol in SYMBOLS:
            owned_quantity = owners.inventory(instance_id, symbol)
            actual = sum((p.quantity for p in account.positions if p.instrument_id == symbol),D(0))
            if owned_quantity != actual:
                raise SafetyBlocked('unowned portfolio symbol position')
        if any(o['symbol'] in SYMBOLS and owners.owner(o['clientOrderId']) !=
               (instance_id, portfolio.allocation_id, o['symbol'])
               for o in self.session.adapter.facts.open_orders):
            raise SafetyBlocked('unowned portfolio symbol order')
        if request.get('action') == 'cancel_pending':
            from factorlab.trading.contracts import TERMINAL
            for key,record in tuple(self.session.gateway.state.orders.items()):
                if (owners.owner(key) == (instance_id, portfolio.allocation_id, record.snapshot.intent.instrument_id)
                        and record.snapshot.status not in TERMINAL):
                    with self.scope(task,record.snapshot.intent.instrument_id, client_id=key):
                        self.session.cancel(key)
            recovered = self.session.gateway.recover(self.session.controller)
            if recovered.reconciled:
                store.record_metadata(done,dict(task_id=task,outcome='CANCEL_RECONCILED'))
            return
        prices, steps = {}, {}
        for symbol in sorted(SYMBOLS):
            with self.scope(task, symbol):
                self.configure_leverage(symbol)
                context = self.session.symbol_context(symbol)
                book = self.session._fresh_book(symbol=symbol)
                current = next((p.quantity for p in account.positions if p.instrument_id == symbol),D(0))
                side = Side.SELL if current * book['bidPrice'] > budget * weights.get(symbol,D(0)) else Side.BUY
                # Bounded marketable LIMIT cushion; account and allocation caps still apply.
                executable = dict(book)
                executable['askPrice'] *= D('1.005')
                executable['bidPrice'] *= D('0.995')
                price = self.session.limit_price(context.rules, side, executable)
                prices[symbol] = price
                steps[symbol] = D(context.rules.filters['LOT_SIZE']['stepSize'])
                self.session.controller.quote(Quote(symbol, price, book['time_utc']))
        def submit(intent):
            if self.active_mailbox_task is not None:
                if utc_instant(self.active_mailbox_task['valid_until']) <= utc_instant(self.session._now()):
                    raise SafetyBlocked('task expired before dispatch; recover original identities')
            with self.scope(task, intent.instrument_id, client_id=intent.client_order_id) as (authorization, guard):
                self.session.submit_instance_intent(intent, instance_id=instance_id,
                    allocation_id=portfolio.allocation_id, authorization=authorization, guard=guard)
        outcome = portfolio.advance(task, prices=prices, steps=steps, submit=submit)
        status = self.session.status()
        owned_ids = {key for key, in store.db.execute(
            'SELECT client_id FROM demo_order_owners WHERE instance_id=? AND allocation_id=?',
            (instance_id, portfolio.allocation_id))}
        status['orders'] = {k:v for k,v in status['orders'].items() if k in owned_ids}
        from .demo_fill_acceptance import receipt_mapping
        mapping = [m for m in receipt_mapping(self.session.adapter)
                   if m['client_order_id'] in owned_ids]
        actual_fees = sum((m['actual_fee'] for m in mapping),D(0))
        realized = sum((D(t['realizedPnl']) for m in mapping for t in m['trades']),D(0))
        funding = sum((e.amount for e in self.session.adapter.funding_events() if e.symbol in SYMBOLS),D(0))
        funds = self.session.adapter.facts.usdt()
        nav = funds['walletBalance'] + funds['unrealizedProfit']
        accounting = dict(wallet=funds['walletBalance'], unrealized=funds['unrealizedProfit'],
            nav=nav, margin_balance=funds['marginBalance'],
            nav_difference=nav-funds['marginBalance'], portfolio_actual_fees=actual_fees,
            portfolio_realized_pnl=realized, portfolio_funding=funding,
            funding_event_observed=any(e.symbol in SYMBOLS for e in self.session.adapter.funding_events()))
        report = dict(task_id=task, outcome=outcome, account=status, accounting=accounting,
                      orders=portfolio.batch_status(task), acceptance_only=True)
        # Reuse the established JSON encoder for Decimal and UTC evidence.
        atomic_json(self.result, json.loads(dumps(report)))
        if outcome in {'NO_EXECUTABLE_DELTA','BATCH_TERMINATED'}:
            store.record_metadata(done,dict(task_id=task,outcome=outcome))
        if self.active_mailbox_task is not None:
            self.receipt('COMPLETED' if outcome == 'NO_EXECUTABLE_DELTA' else
                'REJECTED' if outcome == 'BATCH_TERMINATED' else 'RECONCILING',
                dict(outcome=outcome,orders=report['orders'],accounting={k:str(v) for k,v in accounting.items()}))

    @contextmanager
    def scope(self, task, symbol, *, client_id=None):
        s = self.session
        campaign_key = 'portfolio-campaign:' + identity(task)
        raw = self.runtime._get(campaign_key)
        campaign = json.loads(raw) if raw else None
        now = s._now()
        if campaign is None:
            funds = s.adapter.facts.usdt()
            campaign = dict(id=campaign_key, expires_at=(now+timedelta(hours=1)).isoformat(),
                            opening_equity=str(funds['walletBalance']+funds['unrealizedProfit']))
            with self.runtime.db:
                self.runtime._set(campaign_key,json.dumps(campaign))
        from datetime import datetime
        expires = datetime.fromisoformat(campaign['expires_at'])
        if expires <= now:
            # Renew the window without resetting consumed dispatch identities.
            expires = now+timedelta(hours=1)
            campaign['expires_at'] = expires.isoformat()
            with self.runtime.db:
                self.runtime._set(campaign_key,json.dumps(campaign))
        plan = DemoExecutionPlan(account_id=s.adapter.account_id, symbol=symbol,
            order_types=frozenset({'LIMIT'}), max_quantity=D(1), max_notional=BUDGET,
            max_orders=len([k for k in s.gateway.state.orders if k.startswith('pf')])+12,max_open_orders=4,max_seconds=3600,max_loss=D(2),
            max_margin=ACCOUNT_BUDGET,expires_at=expires,client_id_prefix='pf',
            cleanup='CANCEL_KNOWN_THEN_STOP',recovery='GET_RECONCILE_NO_RESUBMIT')
        def guard(method,params,plan):
            if durable_control_state(self.runtime) is not None:
                raise SafetyBlocked('account execution stopped')
            if self.active_mailbox_task is not None:
                if utc_instant(self.active_mailbox_task['valid_until']) <= utc_instant(s._now()) and method != 'DELETE':
                    raise SafetyBlocked('task expired at dispatch; only known-order cleanup permitted')
            if params.get('symbol') != symbol or symbol not in SYMBOLS:
                raise SafetyBlocked('portfolio symbol ownership mismatch')
            key = ('pf-leverage-'+symbol if 'leverage' in params else
                   params.get('newClientOrderId',params.get('origClientOrderId')))
            if not key or not key.startswith('pf'):
                raise SafetyBlocked('portfolio client identity mismatch')
            if 'leverage' in params and params['leverage'] != 1:
                raise SafetyBlocked('portfolio leverage must be one')
            if 'leverage' not in params and method == 'POST' and params.get('reduceOnly') == 'false':
                funds = s.adapter.facts.usdt()
                if 'opening_equity' not in campaign:
                    raise SafetyBlocked('legacy campaign has no durable loss baseline; use a new task')
                if D(campaign['opening_equity'])-(funds['walletBalance']+funds['unrealizedProfit']) >= D(2):
                    raise SafetyBlocked('portfolio persistent account loss limit')
                state = s.gateway.state
                exposure = sum((abs(p.quantity)*p.mark_price for p in state.positions.values()
                                if p.instrument_id in SYMBOLS),D(0))
                from factorlab.trading.contracts import TERMINAL
                exposure += sum(((r.snapshot.intent.quantity-r.snapshot.cumulative_filled_quantity)
                    * max(r.reservation_price,state.quotes[r.snapshot.intent.instrument_id].price)
                    for r in state.orders.values() if r.snapshot.intent.instrument_id in SYMBOLS
                    and r.snapshot.status not in TERMINAL),D(0))
                if exposure + D(params['quantity'])*D(params['price']) > BUDGET:
                    raise SafetyBlocked('portfolio shared allocation budget exhausted')
            with self.runtime.db:
                self.runtime.db.execute('BEGIN IMMEDIATE')
                if method == 'POST':
                    self.runtime.check_account_budget(params, s.adapter.facts)
                if self.runtime.db.execute('SELECT count(*) FROM dispatches WHERE campaign=?',
                                           (campaign_key,)).fetchone()[0] >= 16:
                    raise SafetyBlocked('portfolio persistent campaign exhausted')
                inserted = self.runtime.db.execute('INSERT OR IGNORE INTO dispatches VALUES(?,?,?,?,NULL)',
                    (campaign_key,method,key,json.dumps(params))).rowcount
                if not inserted:
                    raise SafetyBlocked('portfolio dispatch already consumed; GET recovery required')
        authorization = DemoNetworkAuthorization(plan)
        key = client_id if client_id is not None else ('configuration', symbol)
        s.client.transport.authorizations[key] = authorization
        s._instance_guards[key] = guard
        s._instance_receipt_sinks[key] = lambda receipt: self.record_receipt(campaign_key, receipt)
        yield authorization, guard

    def record_receipt(self,campaign,receipt):
        with self.runtime.db:
            self.runtime.db.execute('UPDATE dispatches SET receipt=? WHERE campaign=? AND method=? AND client_id=?',
                (json.dumps(receipt),campaign,receipt['method'],receipt['client_order_id']))

    def configure_leverage(self,symbol):
        s = self.session
        facts = s.adapter.read_facts()
        config = demo_account_configuration(facts,symbol)
        if config['leverage'] == 1:
            return
        if (any(p.instrument_id == symbol and p.quantity for p in facts.shared().positions)
                or any(o['symbol'] == symbol for o in facts.open_orders)):
            raise SafetyBlocked('leverage configuration conflicts with existing symbol exposure')
        params = dict(symbol=symbol,leverage=1)
        s.client.transport._admission['request'] = ('POST',params,s.adapter.account_id)
        s.client.request('POST','/fapi/v1/leverage',params)
        verified = demo_account_configuration(s.adapter.read_facts(),symbol)
        if verified['leverage'] != 1:
            raise SafetyBlocked('Demo leverage configuration not confirmed')
        s.symbols.observe(symbol)

    def receipt(self,status,detail):
        from .tradfi_tasks import TaskMailbox
        mailbox = TaskMailbox(self.mailbox_path)
        try:
            mailbox.receipt(self.active_mailbox_task['task_id'],recorded_at=self.session._now().isoformat(),
                            status=status,detail=detail)
        finally:
            mailbox.close()
