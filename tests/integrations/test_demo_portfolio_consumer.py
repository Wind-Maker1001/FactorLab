from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading import demo_portfolio_consumer as module
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.control import RiskLimits


def test_portfolio_loss_baseline_survives_runtime_restart(tmp_path, monkeypatch):
    funds = dict(walletBalance=D(5000), unrealizedProfit=D(0))
    facts = SimpleNamespace(usdt=lambda:funds)
    adapter = SimpleNamespace(account_id='DEMO:FactorLab-Demo:unit', facts=facts,
                              read_facts=lambda:facts)
    state = SimpleNamespace(orders={}, positions={})
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={},
        gateway=SimpleNamespace(rules=object(),state=state,leverage=5,max_leverage=5),adapter=adapter,
        client=SimpleNamespace(transport=object()),_write_guard=None,_receipt_sink=None,
        _admit_write=lambda *args:None,controller=SimpleNamespace(limits=RiskLimits()),
        _now=lambda:datetime.now(timezone.utc))
    monkeypatch.setattr(module,'demo_account_configuration',lambda *args:{'leverage':1})
    monkeypatch.setattr(module,'observe_demo_rules',lambda *args,**kwargs:SimpleNamespace(rules=object()))
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    params = dict(symbol='QQQUSDT',side='BUY',reduceOnly='false',quantity='.01',price='600',
                  newClientOrderId='pf-first')
    with consumer.scope('stable-task','QQQUSDT'):
        session._write_guard('POST',params,session.client.transport.authorization.plan)
    runtime.close()
    funds['walletBalance'] -= 3
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    with consumer.scope('stable-task','QQQUSDT'):
        with pytest.raises(SafetyBlocked,match='persistent account loss limit'):
            session._write_guard('POST',dict(params,newClientOrderId='pf-second'),
                                 session.client.transport.authorization.plan)
    assert runtime.db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 1
    runtime.close()


def test_expired_scope_allows_cleanup_but_not_new_dispatch(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    facts = SimpleNamespace(usdt=lambda:dict(walletBalance=D(5000),unrealizedProfit=D(0)))
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={},
        gateway=SimpleNamespace(rules=object(),state=SimpleNamespace(orders={},positions={}),leverage=5,max_leverage=5),
        adapter=SimpleNamespace(account_id='DEMO:FactorLab-Demo:unit',facts=facts,read_facts=lambda:facts),
        client=SimpleNamespace(transport=object()),_write_guard=None,_receipt_sink=None,
        _admit_write=lambda *args:None,controller=SimpleNamespace(limits=RiskLimits()),_now=lambda:now)
    monkeypatch.setattr(module,'demo_account_configuration',lambda *args:{'leverage':1})
    monkeypatch.setattr(module,'observe_demo_rules',lambda *args,**kwargs:SimpleNamespace(rules=object()))
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    consumer.active_mailbox_task = dict(valid_until=(now-timedelta(seconds=1)).isoformat())
    try:
        with consumer.scope('expired-task','QQQUSDT'):
            plan = session.client.transport.authorization.plan
            with pytest.raises(SafetyBlocked,match='task expired'):
                session._write_guard('POST',dict(symbol='QQQUSDT',newClientOrderId='pf-new'),plan)
            session._write_guard('DELETE',dict(symbol='QQQUSDT',origClientOrderId='pf-original'),plan)
        rows = runtime.db.execute('SELECT method,client_id FROM dispatches').fetchall()
        assert rows == [('DELETE','pf-original')]
    finally:
        runtime.close()


@pytest.mark.parametrize('symbol', ['QQQUSDT','SPYUSDT'])
@pytest.mark.parametrize('exceptional_exit', [False, True])
def test_gateway_admits_one_times_portfolio_and_restores_five_times_grid(
        tmp_path, monkeypatch, symbol, exceptional_exit):
    from dataclasses import replace
    from test_binance_futures_trading import setup, gateway_setup
    from factorlab.integrations.binance_futures_trading.fake import NOW, synthetic_rules
    from factorlab.trading import Market, OrderIntent, OrderType, Side
    from factorlab.trading.paper import Instrument, Quote
    adapter, _ = setup()
    monkeypatch.setattr(adapter,'account_id','DEMO:FactorLab-Demo:unit')
    adapter.facts = replace(adapter.facts, account_id=adapter.account_id, symbol_config=(
        dict(symbol='NVDAUSDT',leverage=5,marginType='ISOLATED'),
        dict(symbol=symbol,leverage=1,marginType='CROSSED')))
    gateway, controller = gateway_setup(adapter)
    gateway.leverage = gateway.max_leverage = 5
    controller._atomic(lambda:gateway.state.instruments.update({symbol:Instrument(symbol,'USDT')}))
    controller.quote(Quote(symbol,D(100),NOW))
    monkeypatch.setattr(adapter,'read_facts',lambda:adapter.facts)
    rules = replace(synthetic_rules(),symbol=symbol)
    monkeypatch.setattr(module,'observe_demo_rules',lambda *args,**kwargs:SimpleNamespace(rules=rules))
    original_config = module.demo_account_configuration(adapter.facts,'NVDAUSDT')
    session = SimpleNamespace(symbol='NVDAUSDT',configuration=original_config,
        gateway=gateway,adapter=adapter,client=SimpleNamespace(transport=object()),
        _write_guard=None,_receipt_sink=None,_admit_write=lambda *args:None,
        controller=controller,_now=lambda:NOW)
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    class Admitted(Exception):
        pass
    def dispatch(intent):
        raise Admitted
    monkeypatch.setattr(adapter,'submit_order',dispatch)
    intent = OrderIntent(Market.BINANCE_USDM,symbol,Side.BUY,OrderType.LIMIT,D('.05'),D(100),'pf-test')
    try:
        with pytest.raises(SafetyBlocked,match='leverage/margin'):
            gateway.submit_order(intent)
        if exceptional_exit:
            with pytest.raises(Admitted):
                with consumer.scope('leverage-task',symbol):
                    assert session.configuration['margin_type'] == 'CROSSED'
                    gateway.submit_order(intent)
        else:
            with consumer.scope('leverage-task',symbol):
                with pytest.raises(Admitted):
                    gateway.submit_order(intent)
        assert (gateway.leverage,gateway.max_leverage) == (5,5)
        assert session.symbol == 'NVDAUSDT'
        assert session.configuration == original_config
        with pytest.raises(SafetyBlocked,match='leverage/margin'):
            gateway.submit_order(intent)
    finally:
        runtime.close()
        controller.store.close()
        adapter.db.close()


def mailbox_consumer(tmp_path, monkeypatch, *, expired=False):
    from factorlab.integrations.binance_futures_trading.tradfi_tasks import TaskMailbox, PortfolioTask
    from factorlab.trading.storage import PaperStore
    now = datetime.now(timezone.utc)
    store = PaperStore(tmp_path / 'controller.sqlite3')
    funds = dict(walletBalance=D(5000), unrealizedProfit=D(0))
    facts = SimpleNamespace(usdt=lambda:funds, open_orders=[])
    state = SimpleNamespace(orders={}, positions={}, instruments={})
    account = SimpleNamespace(reconciled=True, positions=[])
    controller = SimpleNamespace(store=store, _atomic=lambda f:f(), quote=lambda q:None)
    session = SimpleNamespace(controller=controller,
        adapter=SimpleNamespace(read_facts=lambda:facts, facts=facts),
        gateway=SimpleNamespace(state=state, recover=lambda c:account,
            rules=SimpleNamespace(filters={'LOT_SIZE':{'stepSize':'.01'}})),
        _now=lambda:now, refresh_rules=lambda:None,
        _fresh_book=lambda:dict(bidPrice=D(600),askPrice=D(601),time_utc=now),
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
    monkeypatch.setattr(consumer,'scope',lambda *args:nullcontext())
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
    monkeypatch.setattr(resumed,'scope',lambda *args:nullcontext())
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
