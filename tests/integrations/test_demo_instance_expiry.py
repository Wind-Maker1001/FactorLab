from types import SimpleNamespace

from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.trading.storage import PaperStore, dumps
from factorlab.trading.portfolio_execution import PortfolioStore


def test_expiry_freezes_existing_plan_before_order_cleanup(tmp_path):
    store = PaperStore(tmp_path / 'owner.sqlite3')
    capital = PortfolioStore(tmp_path / 'capital.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)', ('decision', dumps({})))
    capital.db.execute('INSERT INTO portfolio_plans(id,account,strategy,session,payload) VALUES(?,?,?,?,?)',
                       ('plan', 'account', 'instance', 'session',
                        dumps(dict(id='plan', request=dict(rebalance_id='decision')))))
    calls = []
    phase = ['WAITING']
    def expire(plan_id):
        calls.append(plan_id)
        return SimpleNamespace(phase=phase[0])
    session = SimpleNamespace()
    execution = SimpleNamespace(session=session, store=store,
        executor=SimpleNamespace(store=capital, expire=expire))
    owner = DemoInstanceOwner(session, execution)
    try:
        owner._expire_task('decision', {})
        assert store.db.execute('SELECT completed FROM demo_instance_outbox').fetchone()[0] == 0
        phase[0] = 'COMPLETE'
        owner._expire_task('decision', {})
        assert store.db.execute('SELECT completed FROM demo_instance_outbox').fetchone()[0] == 2
        assert calls == ['plan', 'plan']
    finally:
        capital.close()
        store.close()


def test_expired_inflight_mailbox_task_still_checks_immutable_content(tmp_path):
    from datetime import timedelta
    from decimal import Decimal as D
    import pytest
    from factorlab.trading.realtime import identity
    from factorlab.integrations.binance_futures_trading.demo_instance_execution import DemoInstanceExecutionAdapter
    from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
    from test_binance_demo_execution import NOW
    store = PaperStore(tmp_path / 'identity.sqlite3')
    binding = dict(account_id='account', instance_id='one', allocation_id='funded')
    controller = SimpleNamespace(store=store, broker=None)
    session = SimpleNamespace(controller=controller, adapter=SimpleNamespace(account_id='account'),
                              _now=lambda: NOW)
    execution = DemoInstanceExecutionAdapter(session)
    owner = DemoInstanceOwner(session, execution)
    store.record_metadata('demo:instance:one', dict(binding=binding, symbols=['BTCUSDT'],
        allocation=dict(budget_usdt=D(20))))
    task = dict(task_id='task', allocation_id='funded', equity_usdt='10',
                weights={'BTCUSDT': '.4'}, valid_until=(NOW-timedelta(seconds=1)).isoformat())
    receipt = dict(binding=binding, package_id='mailbox-target-v1', event_id='task',
        input_identity=identity(task), event=dict(task=task), decision=dict(kind='target_weights',
        targets={'BTCUSDT': D('.2')}, orders=[], cancellations=[], state={}))
    key = 'demo:decision:' + identity(('one', 'mailbox-target-v1', 'task'))
    store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)',
                     (key, dumps(dict(receipt=receipt))))
    try:
        assert owner.accept_task(task)[0] == 'RECONCILING'
        with pytest.raises(SafetyBlocked, match='conflicting content'):
            owner.accept_task(dict(task, equity_usdt='9'))
    finally:
        store.close()
