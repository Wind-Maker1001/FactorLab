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
from urllib.parse import parse_qs

from factorlab.trading.contracts import Side
from factorlab.trading.paper import Instrument, Quote
from factorlab.trading.portfolio_runtime import PortfolioRuntime
from factorlab.trading.realtime import identity
from factorlab.trading.storage import dumps
from .demo_cloud_storage import atomic_json
from .demo_runtime import durable_control_state
from .demo_session import demo_account_configuration
from .execution import observe_demo_rules
from .network import DemoExecutionPlan, DemoNetworkAuthorization, DemoNetworkTransport
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

    def poll(self):
        try:
            self._poll()
        except Exception as error:
            atomic_json(self.result,dict(status='PORTFOLIO_BLOCKED',reason=str(error)[:300]))

    def _poll(self):
        if durable_control_state(self.runtime) is not None:
            return
        requests = [p for p in sorted(self.directory.glob('*.json'))
            if self.session.controller.store.event_payload('portfolio-result:' + identity(
                json.loads(p.read_text())['task_id'])) is None]
        if not requests:
            return
        request = json.loads(requests[0].read_text())
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
            return
        self.session.shared_account = True
        store.record_metadata('demo:static-shared-account:v1',
            dict(grid=['NVDAUSDT'], portfolio=sorted(SYMBOLS), grid_budget=D(7), portfolio_budget=BUDGET))
        self.session.controller._atomic(lambda: self.session.gateway.state.instruments.update(
            {s:Instrument(s,'USDT') for s in SYMBOLS}))
        account = self.session.gateway.recover(self.session.controller)
        if not account.reconciled:
            return
        for symbol in SYMBOLS:
            owned_quantity = sum(((r.snapshot.cumulative_filled_quantity if r.snapshot.intent.side is Side.BUY else -r.snapshot.cumulative_filled_quantity)
                for k,r in self.session.gateway.state.orders.items()
                if k.startswith('pf') and r.snapshot.intent.instrument_id == symbol),D(0))
            actual = sum((p.quantity for p in account.positions if p.instrument_id == symbol),D(0))
            if owned_quantity != actual:
                raise SafetyBlocked('unowned portfolio symbol position')
        if any(o['symbol'] in SYMBOLS and not o['clientOrderId'].startswith('pf')
               for o in self.session.adapter.facts.open_orders):
            raise SafetyBlocked('unowned portfolio symbol order')
        prices, steps = {}, {}
        for symbol in sorted(SYMBOLS):
            with self.scope(task, symbol):
                self.configure_leverage(symbol)
                self.session.refresh_rules()
                book = self.session._fresh_book()
                current = next((p.quantity for p in account.positions if p.instrument_id == symbol),D(0))
                side = Side.SELL if current * book['bidPrice'] > budget * weights.get(symbol,D(0)) else Side.BUY
                price = self.session.limit_price(self.session.gateway.rules, side, book)
                prices[symbol] = price
                steps[symbol] = D(self.session.gateway.rules.filters['LOT_SIZE']['stepSize'])
                self.session.controller.quote(Quote(symbol, price, book['time_utc']))
        portfolio = PortfolioRuntime(self.session.controller, allocation_id='tradfi-demo-acceptance',
            owned_symbols=SYMBOLS, recover=lambda:self.session.gateway.recover(self.session.controller))
        portfolio.begin(task, weights=weights, equity=budget)
        def submit(intent):
            with self.scope(task, intent.instrument_id):
                self.session.submit_intent(intent)
        outcome = portfolio.advance(task, prices=prices, steps=steps, submit=submit)
        status = self.session.status()
        status['orders'] = {k:v for k,v in status['orders'].items() if k.startswith('pf')}
        report = dict(task_id=task, outcome=outcome, account=status,
                      orders=portfolio.batch_status(task), acceptance_only=True)
        # Reuse the established JSON encoder for Decimal and UTC evidence.
        atomic_json(self.result, json.loads(dumps(report)))
        if outcome in {'NO_EXECUTABLE_DELTA','BATCH_TERMINATED'}:
            store.record_metadata(done,dict(task_id=task,outcome=outcome))

    @contextmanager
    def scope(self, task, symbol):
        s = self.session
        original = (s.symbol,s.configuration,s.gateway.rules,s.client.transport,
                    s._write_guard,s._receipt_sink,s.controller.limits,
                    getattr(s.adapter,'_rule_observation',None))
        campaign_key = 'portfolio-campaign:' + identity(task)
        raw = self.runtime._get(campaign_key)
        campaign = json.loads(raw) if raw else None
        now = s._now()
        if campaign is None:
            campaign = dict(id=campaign_key, expires_at=(now+timedelta(hours=1)).isoformat())
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
            max_orders=12,max_open_orders=4,max_seconds=3600,max_loss=D(2),
            max_margin=ACCOUNT_BUDGET,expires_at=expires,client_id_prefix='pf',
            cleanup='CANCEL_KNOWN_THEN_STOP',recovery='GET_RECONCILE_NO_RESUBMIT')
        def guard(method,params,plan):
            if durable_control_state(self.runtime) is not None:
                raise SafetyBlocked('account execution stopped')
            if params.get('symbol') != symbol or symbol not in SYMBOLS:
                raise SafetyBlocked('portfolio symbol ownership mismatch')
            key = ('pf-leverage-'+symbol if 'leverage' in params else
                   params.get('newClientOrderId',params.get('origClientOrderId')))
            if not key or not key.startswith('pf'):
                raise SafetyBlocked('portfolio client identity mismatch')
            if 'leverage' in params and params['leverage'] != 1:
                raise SafetyBlocked('portfolio leverage must be one')
            with self.runtime.db:
                self.runtime.db.execute('BEGIN IMMEDIATE')
                if self.runtime.db.execute('SELECT count(*) FROM dispatches WHERE campaign=?',
                                           (campaign_key,)).fetchone()[0] >= 16:
                    raise SafetyBlocked('portfolio persistent campaign exhausted')
                inserted = self.runtime.db.execute('INSERT OR IGNORE INTO dispatches VALUES(?,?,?,?,NULL)',
                    (campaign_key,method,key,json.dumps(params))).rowcount
                if not inserted:
                    raise SafetyBlocked('portfolio dispatch already consumed; GET recovery required')
        def observe(request,response):
            if request.path != '/fapi/v1/leverage':
                s._observe_response(request,response)
                return
            receipt = dict(method='POST',client_order_id='pf-leverage-'+symbol,
                http_status=response.status if response else None,
                outcome_unknown=response is None, symbol=symbol, leverage=1)
            with self.runtime.db:
                self.runtime.db.execute('UPDATE dispatches SET receipt=? WHERE campaign=? AND client_id=?',
                    (json.dumps(receipt),campaign_key,receipt['client_order_id']))
        try:
            s.symbol = symbol
            s.configuration = demo_account_configuration(s.adapter.read_facts(),symbol)
            observation = observe_demo_rules(s.adapter,symbol,source_version='OFFICIAL_DEMO')
            s.gateway.rules = observation.rules
            s._write_guard = guard
            s._receipt_sink = lambda receipt: self.record_receipt(campaign_key,receipt)
            s.client.transport = DemoNetworkTransport(authorization=DemoNetworkAuthorization(plan),
                write_guard=s._admit_write,observe_response=observe)
            s.controller.limits = replace(original[6], max_order_notional=BUDGET,
                max_instrument_exposure=BUDGET,account_budget=ACCOUNT_BUDGET)
            yield
        finally:
            (s.symbol,s.configuration,s.gateway.rules,s.client.transport,s._write_guard,
             s._receipt_sink,s.controller.limits,s.adapter._rule_observation) = original
            s.controller.limits = replace(s.controller.limits,account_budget=ACCOUNT_BUDGET)

    def record_receipt(self,campaign,receipt):
        with self.runtime.db:
            self.runtime.db.execute('UPDATE dispatches SET receipt=? WHERE campaign=? AND method=? AND client_id=?',
                (json.dumps(receipt),campaign,receipt['method'],receipt['client_order_id']))

    def configure_leverage(self,symbol):
        s = self.session
        facts = s.adapter.read_facts()
        config = demo_account_configuration(facts,symbol)
        if config['leverage'] == 1:
            s.configuration = config
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
        s.configuration = verified
