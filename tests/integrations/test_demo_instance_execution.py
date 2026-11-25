from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import setup
from factorlab.integrations.binance_futures_trading.demo_instance_execution import DemoInstanceExecutionAdapter
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading import Market
from factorlab.trading.storage import decode
import json


def test_quantity_delta_receipt_replay_keeps_original_intents():
    adapter, gateway, controller, _ = setup()
    session = SimpleNamespace(adapter=adapter, controller=controller,
        symbol_context=lambda symbol:SimpleNamespace(rules=gateway.rules),
        _fresh_book=lambda **kwargs:dict(askPrice=D(100),bidPrice=D(100)),
        limit_price=lambda *args:D(100))
    admissions = []
    executor = SimpleNamespace(capital_policy=SimpleNamespace(
        qualify=lambda *args: admissions.append(args) or
            dict(allocation_id='funded-one', capital=D(7), initial_cash=D(7))))
    execution = DemoInstanceExecutionAdapter(session, executor=executor)
    binding = dict(market=Market.BINANCE_USDM.value, account_id=adapter.account_id,
        instance_id='one', allocation_id='funded-one', strategy_id='grid')
    spec = SimpleNamespace(capital=dict(currency='USDT',requested=D(7)),
        symbols=['NVDAUSDT'], execution=dict(order_types=['LIMIT'],time_in_force=['GTC'],shorting=False))
    allocation = dict(instance_id='one',allocation_id='funded-one',budget_usdt=D(7))
    with pytest.raises(SafetyBlocked, match='shared capital admission policy'):
        DemoInstanceExecutionAdapter(session).qualify(spec, binding, allocation)
    execution.qualify(spec, binding, allocation)
    assert admissions == [(spec, binding, allocation)]
    receipt = dict(binding=binding,package_id='package',event_id='bar:1',input_identity='input',
        event={},decision=dict(kind='quantity_deltas',targets={'NVDAUSDT':D('.05')},
            orders=[],cancellations=[],state={}))
    try:
        from test_binance_demo_execution import intent
        from factorlab.trading import Side
        short_receipt = dict(receipt, event_id='short-attempt', decision=dict(kind='orders', targets={},
            orders=[intent('offered-short', side=Side.SELL, limit='100', quantity='.05')],
            cancellations=[], state={}))
        with pytest.raises(SafetyBlocked, match='not declared shorting'):
            execution.execute(short_receipt)
        assert controller.store.db.execute('SELECT count(*) FROM demo_instance_outbox').fetchone()[0] == 0
        execution.execute(receipt)
        execution.execute(receipt)
        rows = controller.store.db.execute('SELECT payload FROM demo_instance_outbox').fetchall()
        assert len(rows) == 1
        payload = decode(json.loads(rows[0][0]))
        assert payload['intents'][0].quantity == D('.05')
        assert execution.owners.owner(payload['intents'][0].client_order_id) == ('one','funded-one','NVDAUSDT')
        with pytest.raises(SafetyBlocked, match='conflicting content'):
            execution.execute(dict(receipt,input_identity='different'))
        with pytest.raises(SafetyBlocked, match='previous decision'):
            execution.execute(dict(receipt,event_id='bar:2'))
        assert not adapter.db.execute('SELECT 1 FROM futures_orders').fetchone()
        from datetime import datetime, timezone
        calls = []
        execution.executor = SimpleNamespace(
            store=SimpleNamespace(allocation=lambda account, instance:
                dict(allocation_id='funded-one', capital=D(7))),
            create=lambda **kwargs: calls.append(kwargs) or 'shared-plan')
        decision_id = controller.store.db.execute('SELECT id FROM demo_instance_outbox').fetchone()[0]
        now = datetime.now(timezone.utc)
        assert execution.prepare_plan(decision_id, now=now) == 'shared-plan'
        assert execution.prepare_plan(decision_id, now=now) == 'shared-plan'
        assert calls[0] == calls[1]
        assert calls[0]['strategy_id'] == 'one'
        assert calls[0]['direct_orders'] == [(payload['intents'][0], D(100))]
        execution.executor.run_once = lambda *args, **kwargs: SimpleNamespace(phase='WAITING')
        execution.advance(decision_id, equity=D(5000), quotes={}, rules={}, now=now)
        assert controller.store.db.execute('SELECT completed FROM demo_instance_outbox').fetchone()[0] == 0
        execution.executor.run_once = lambda *args, **kwargs: SimpleNamespace(phase='COMPLETE')
        execution.advance(decision_id, equity=D(5000), quotes={}, rules={}, now=now)
        assert controller.store.db.execute('SELECT completed FROM demo_instance_outbox').fetchone()[0] == 1
        execution.executor.store.allocation = lambda account, instance: None
        with pytest.raises(SafetyBlocked, match='existing shared capital allocation'):
            execution.prepare_plan(decision_id, now=now)
    finally:
        controller.store.close()
        adapter.store.close()
