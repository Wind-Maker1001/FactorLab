from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading import demo_portfolio_consumer as module
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.control import RiskLimits


@pytest.mark.parametrize('global_exhausted', [False, True])
def test_portfolio_loss_baseline_survives_runtime_restart(tmp_path, monkeypatch, global_exhausted):
    funds = dict(walletBalance=D(5000), unrealizedProfit=D(0))
    facts = SimpleNamespace(usdt=lambda:funds)
    adapter = SimpleNamespace(account_id='DEMO:FactorLab-Demo:unit', facts=facts,
                              read_facts=lambda:facts)
    state = SimpleNamespace(orders={}, positions={})
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={},
        gateway=SimpleNamespace(rules=object(),state=state,leverage=5,max_leverage=5),adapter=adapter,
        client=SimpleNamespace(transport=SimpleNamespace(authorizations={})),_write_guard=None,_receipt_sink=None,_instance_guards={},_instance_receipt_sinks={},
        _admit_write=lambda *args:None,controller=SimpleNamespace(limits=RiskLimits()),
        _now=lambda:datetime.now(timezone.utc))
    monkeypatch.setattr(module,'demo_account_configuration',lambda *args:{'leverage':1})
    runtime = DemoRuntime(tmp_path)
    runtime.start_continuous(dict(account_id=adapter.account_id, configuration={}),
        opening_equity=D(5000), stop_epoch=runtime.stop_epoch())
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    params = dict(symbol='QQQUSDT',side='BUY',reduceOnly='false',quantity='.01',price='600',
                  newClientOrderId='pf-first')
    if global_exhausted:
        import json
        runtime._set('continuous', json.dumps(dict(id='continuous-test', state='RUNNING',
            total_orders=100000, total_notional='5', opening_equity='5000', max_loss='250')))
        runtime.db.commit()
        with consumer.scope('stable-task','QQQUSDT', client_id='pf-first') as (authorization, guard):
            with pytest.raises(SafetyBlocked, match='global Demo trading budget'):
                guard('POST', params, authorization.plan)
        assert runtime.db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 0
        runtime.close()
        return
    original = (session.symbol, session.configuration, session.gateway.rules, session.client.transport)
    with consumer.scope('stable-task','QQQUSDT', client_id='pf-first') as (authorization, guard):
        guard('POST',params,authorization.plan)
    assert (session.symbol, session.configuration, session.gateway.rules, session.client.transport) == original
    runtime.close()
    funds['walletBalance'] -= 3
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    with consumer.scope('stable-task','QQQUSDT', client_id='pf-second') as (authorization, guard):
        with pytest.raises(SafetyBlocked,match='persistent account loss limit'):
            guard('POST',dict(params,newClientOrderId='pf-second'),authorization.plan)
    assert runtime.db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 1
    runtime.close()


def test_expired_scope_allows_cleanup_but_not_new_dispatch(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    facts = SimpleNamespace(usdt=lambda:dict(walletBalance=D(5000),unrealizedProfit=D(0)))
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={},
        gateway=SimpleNamespace(rules=object(),state=SimpleNamespace(orders={},positions={}),leverage=5,max_leverage=5),
        adapter=SimpleNamespace(account_id='DEMO:FactorLab-Demo:unit',facts=facts,read_facts=lambda:facts),
        client=SimpleNamespace(transport=SimpleNamespace(authorizations={})),_write_guard=None,_receipt_sink=None,_instance_guards={},_instance_receipt_sinks={},
        _admit_write=lambda *args:None,controller=SimpleNamespace(limits=RiskLimits()),_now=lambda:now)
    monkeypatch.setattr(module,'demo_account_configuration',lambda *args:{'leverage':1})
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    consumer.active_mailbox_task = dict(valid_until=(now-timedelta(seconds=1)).isoformat())
    try:
        with consumer.scope('expired-task','QQQUSDT') as (authorization, guard):
            plan = authorization.plan
            with pytest.raises(SafetyBlocked,match='task expired'):
                guard('POST',dict(symbol='QQQUSDT',newClientOrderId='pf-new'),plan)
            guard('DELETE',dict(symbol='QQQUSDT',origClientOrderId='pf-original'),plan)
        rows = runtime.db.execute('SELECT method,client_id FROM dispatches').fetchall()
        assert rows == [('DELETE','pf-original')]
    finally:
        runtime.close()


@pytest.mark.parametrize('symbol', ['QQQUSDT','SPYUSDT'])
@pytest.mark.parametrize('exceptional_exit', [False, True])
def test_gateway_admits_one_times_portfolio_and_restores_five_times_grid(
        tmp_path, monkeypatch, symbol, exceptional_exit):
    from test_binance_demo_execution import setup
    from test_demo_symbols import SymbolsFixture
    from factorlab.integrations.binance_futures_trading.fake import NOW
    from factorlab.integrations.binance_futures_trading.demo_symbols import DemoSymbolRegistry
    from factorlab.trading import Market, OrderIntent, OrderType, Side
    from factorlab.trading.paper import Instrument, Quote
    fake = SymbolsFixture()
    fake.symbols['NVDAUSDT'] = (5, '.01', '.01')
    fake.symbols[symbol] = (1, '.01', '.01')
    adapter, gateway, controller, _ = setup(fake=fake)
    registry = DemoSymbolRegistry(adapter, controller.store)
    original_rules = gateway.rules
    original_leverage = (gateway.leverage, gateway.max_leverage)
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={'leverage':5},
        gateway=gateway,adapter=adapter,client=SimpleNamespace(transport=SimpleNamespace(authorizations={})),
        _instance_guards={},_instance_receipt_sinks={},controller=controller,_now=lambda:NOW)
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    class Admitted(Exception):
        pass
    monkeypatch.setattr(adapter,'submit_order',lambda intent: (_ for _ in ()).throw(Admitted()))
    intent = OrderIntent(Market.BINANCE_USDM,symbol,Side.BUY,OrderType.LIMIT,D('.05'),D(100),'pf-test')
    try:
        with pytest.raises(SafetyBlocked,match='observed Demo rules'):
            gateway.execution_rules(symbol)
        gateway.register_symbol(registry.observe(symbol))
        controller._atomic(lambda:gateway.state.instruments.update({symbol:Instrument(symbol,'USDT')}))
        controller.quote(Quote(symbol,D(100),NOW))
        if exceptional_exit:
            with pytest.raises(Admitted):
                with consumer.scope('leverage-task',symbol,client_id='pf-test'):
                    gateway.submit_order(intent)
        else:
            with consumer.scope('leverage-task',symbol,client_id='pf-test'):
                with pytest.raises(Admitted):
                    gateway.submit_order(intent)
        assert (gateway.leverage,gateway.max_leverage) == original_leverage
        assert gateway.rules is original_rules
        assert gateway.execution_rules(symbol)[1:] == (1,1)
        assert session.symbol == 'NVDAUSDT'
        assert session.configuration == {'leverage':5}
    finally:
        runtime.close()
        controller.store.close()
        adapter.store.close()


def mailbox_consumer(tmp_path, monkeypatch, *, expired=False):
    from factorlab.integrations.binance_futures_trading.tradfi_tasks import TaskMailbox, PortfolioTask
    from factorlab.trading.storage import PaperStore
    now = datetime.now(timezone.utc)
    store = PaperStore(tmp_path / 'controller.sqlite3')
    funds = dict(walletBalance=D(5000), unrealizedProfit=D(0))
    facts = SimpleNamespace(usdt=lambda:funds, open_orders=[])
    state = SimpleNamespace(orders={}, positions={}, instruments={})
    account = SimpleNamespace(reconciled=True, positions=[])
    controller = SimpleNamespace(store=store, limits=RiskLimits(), _atomic=lambda f:f(), quote=lambda q:None)
    session = SimpleNamespace(controller=controller,
        adapter=SimpleNamespace(read_facts=lambda:facts, facts=facts),
        gateway=SimpleNamespace(state=state, recover=lambda c:account,
            rules=SimpleNamespace(filters={'LOT_SIZE':{'stepSize':'.01'}})),
        _now=lambda:now, refresh_rules=lambda:None,
        symbol_context=lambda symbol:SimpleNamespace(rules=SimpleNamespace(filters={'LOT_SIZE':{'stepSize':'.01'}})),
        _fresh_book=lambda **kwargs:dict(bidPrice=D(600),askPrice=D(601),time_utc=now),
        limit_price=lambda rules,side,book:book['askPrice'])
    controller.broker = session.gateway
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    task = PortfolioTask(task_id='regression-task', allocation_id='tradfi-qqq-spy-test',
        session='2026-11-24',observed_at=(now-timedelta(minutes=2)).isoformat(),
        valid_until=(now+timedelta(seconds=-1 if expired else 60)).isoformat(),
        equity_usdt=D(5),weights={'QQQUSDT':D('.5'),'SPYUSDT':D('.5')},task_type='ACCEPTANCE')
    mailbox = TaskMailbox(consumer.mailbox_path)
    mailbox.submit(task)
    monkeypatch.setattr(consumer,'scope',lambda *args,**kwargs:nullcontext())
    monkeypatch.setattr(consumer,'configure_leverage',lambda symbol:None)
    return consumer, mailbox, store, runtime, funds


@pytest.mark.parametrize('resumed', [False, True])
def test_mailbox_binds_requested_equity_once(tmp_path, monkeypatch, resumed):
    from factorlab.trading.portfolio_runtime import PortfolioRuntime
    consumer, mailbox, store, runtime, funds = mailbox_consumer(tmp_path,monkeypatch)
    captured = []
    original_begin = PortfolioRuntime.begin
    def begin(self, task, **target):
        captured.append(target['equity'])
        return original_begin(self,task,**target)
    if resumed:
        portfolio = PortfolioRuntime(consumer.session.controller,
            allocation_id='tradfi-qqq-spy-test',owned_symbols=module.SYMBOLS,recover=lambda:None)
        portfolio.begin('regression-task',weights={'QQQUSDT':D('.5'),'SPYUSDT':D('.5')},equity=D(5))
        funds['walletBalance'] = D('11.999')
    monkeypatch.setattr(PortfolioRuntime,'begin',begin)
    class TargetCaptured(Exception):
        pass
    def advance(*args, **kwargs):
        raise TargetCaptured
    monkeypatch.setattr(PortfolioRuntime,'advance',advance)
    try:
        with pytest.raises(TargetCaptured):
            consumer._poll()
        assert captured == [D(5)]
    finally:
        mailbox.close()
        store.close()
        runtime.close()


def test_budget_survives_restart_before_first_exchange_configuration(tmp_path, monkeypatch):
    import json
    from factorlab.trading.storage import PaperStore, decode
    from factorlab.trading.realtime import identity
    consumer, mailbox, store, runtime, funds = mailbox_consumer(tmp_path,monkeypatch)
    funds['walletBalance'] = D(11)
    class Interrupted(Exception):
        pass
    def interrupt(symbol):
        raise Interrupted
    monkeypatch.setattr(consumer,'configure_leverage',interrupt)
    with pytest.raises(Interrupted):
        consumer._poll()
    key = 'portfolio:'+identity(('tradfi-qqq-spy-test','regression-task'))
    assert decode(json.loads(store.event_payload(key)))['equity'] == D(4)
    assert mailbox.snapshot()[0]['detail']['owner_budget_usdt'] == '4'
    store.close()
    runtime.close()
    funds.update(walletBalance=D('10.999'),unrealizedProfit=D('-.2'))
    store = PaperStore(tmp_path / 'controller.sqlite3')
    runtime = DemoRuntime(tmp_path)
    session = consumer.session
    session.controller.store = store
    resumed = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    monkeypatch.setattr(resumed,'scope',lambda *args,**kwargs:nullcontext())
    monkeypatch.setattr(resumed,'configure_leverage',interrupt)
    try:
        with pytest.raises(Interrupted):
            resumed._poll()
        assert decode(json.loads(store.event_payload(key)))['equity'] == D(4)
    finally:
        mailbox.close()
        store.close()
        runtime.close()


def test_expiry_cancels_only_original_batch_orders(tmp_path, monkeypatch):
    from factorlab.trading.contracts import OrderStatus
    from factorlab.trading.portfolio_runtime import PortfolioRuntime
    consumer, mailbox, store, runtime, _ = mailbox_consumer(tmp_path,monkeypatch,expired=True)
    portfolio = PortfolioRuntime(consumer.session.controller,
        allocation_id='tradfi-qqq-spy-test',owned_symbols=module.SYMBOLS,recover=lambda:None)
    portfolio.begin('regression-task',weights={'QQQUSDT':D('.5'),'SPYUSDT':D('.5')},equity=D(5))
    snapshot = SimpleNamespace(status=OrderStatus.NEW)
    consumer.session.gateway.state.orders['pf-original'] = SimpleNamespace(snapshot=snapshot)
    monkeypatch.setattr(PortfolioRuntime,'batch_status',lambda *args:[
        dict(client_order_id='pf-original',symbol='QQQUSDT')])
    canceled = []
    def cancel(key):
        canceled.append(key)
        snapshot.status = OrderStatus.CANCELED
    consumer.session.cancel = cancel
    try:
        consumer._poll()
        assert canceled == ['pf-original']
        assert mailbox.snapshot()[0]['status'] == 'REJECTED'
    finally:
        mailbox.close()
        store.close()
        runtime.close()


def test_durable_completion_wins_over_expiry(tmp_path, monkeypatch):
    from factorlab.trading.realtime import identity
    consumer, mailbox, store, runtime, _ = mailbox_consumer(tmp_path,monkeypatch,expired=True)
    store.record_metadata('portfolio-result:'+identity('regression-task'),
        dict(task_id='regression-task',outcome='NO_EXECUTABLE_DELTA'))
    try:
        consumer._poll()
        assert mailbox.snapshot()[0]['status'] == 'COMPLETED'
    finally:
        mailbox.close()
        store.close()
        runtime.close()
