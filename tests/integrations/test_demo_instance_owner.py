from types import SimpleNamespace

from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore


def test_expired_unsent_task_is_rejected_without_submission(tmp_path):
    from datetime import timedelta
    from factorlab.trading.storage import PaperStore, dumps
    from factorlab.trading.portfolio_execution import PortfolioStore, SubmissionNotAttempted
    from test_binance_demo_execution import intent, NOW
    import pytest
    store = PaperStore(tmp_path / 'expired.sqlite3')
    capital = PortfolioStore(tmp_path / 'expired-capital.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    def absent(client_id):
        raise SubmissionNotAttempted(client_id)
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='account'))
    executor = SimpleNamespace(store=capital, adapter=SimpleNamespace(query_order=absent,
        cancel_order=lambda *args: pytest.fail('unsent order cannot require cancellation')))
    execution = SimpleNamespace(session=session, store=store, executor=executor,
        advance=lambda *args, **kwargs: pytest.fail('expired task cannot submit'))
    owner = DemoInstanceOwner(session, execution)
    accepted = dict(receipt=dict(event=dict(task=dict(valid_until=(NOW-timedelta(seconds=1)).isoformat()))),
                    intents=[intent('original')])
    store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)', ('expired', dumps(accepted)))
    try:
        assert owner.advance(equity=5000, quotes={}, rules={}, now=NOW) == {}
        assert store.db.execute('SELECT completed FROM demo_instance_outbox').fetchone()[0] == 2
    finally:
        capital.close()
        store.close()


def test_mailbox_targets_use_only_existing_instance_capital(tmp_path):
    from decimal import Decimal as D
    from datetime import timedelta
    from factorlab.trading.storage import PaperStore
    from test_binance_demo_execution import NOW
    from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
    import pytest
    store = PaperStore(tmp_path / 'tasks.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    binding = dict(account_id='account', instance_id='one', allocation_id='funded')
    store.record_metadata('demo:instance:one', dict(binding=binding,
        symbols=['BTCUSDT', 'ETHUSDT'], allocation=dict(budget_usdt=D(20))))
    receipts = []
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='account'), _now=lambda: NOW)
    capital = PortfolioStore(tmp_path / 'tasks-capital.sqlite3')
    capital.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                       ('account', 'one', 'funded', '20', '20'))
    executor = PortfolioExecutor(None, capital)
    execution = SimpleNamespace(session=session, store=store, executor=executor, execute=receipts.append)
    owner = DemoInstanceOwner(session, execution)
    task = dict(task_id='task', allocation_id='funded', equity_usdt='10',
        weights={'BTCUSDT': '.4', 'ETHUSDT': '.4'}, valid_until=(NOW+timedelta(minutes=1)).isoformat())
    try:
        status, _ = owner.accept_task(task)
        assert status == 'ACCEPTED'
        assert receipts[0]['decision']['targets'] == {'BTCUSDT': D('.4'), 'ETHUSDT': D('.4')}
        assert receipts[0]['event']['task']['equity_usdt'] == '10'
        with pytest.raises(SafetyBlocked, match='funded instance'):
            owner.accept_task(dict(task, equity_usdt='21'))
        assert owner.accept_task(dict(task, valid_until=NOW.isoformat()))[0] == 'REJECTED'
        assert len(receipts) == 1
    finally:
        capital.close()
        store.close()


def test_open_uses_existing_store_without_funding_instances(tmp_path):
    from factorlab.trading.portfolio_execution import PortfolioStore
    from test_binance_demo_execution import setup, NOW
    adapter, gateway, controller, _ = setup()
    capital = PortfolioStore(tmp_path / 'capital.sqlite3')
    session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller,
                              _now=lambda: NOW, instance_owner=None)
    try:
        owner = DemoInstanceOwner.open(session, capital, capability=lambda *args: None)
        assert session.instance_owner is owner
        assert owner.execution.executor.store is capital
        assert owner.execution.executor.adapter.session is session
        assert capital.db.execute('SELECT count(*) FROM portfolio_allocations').fetchone()[0] == 0
        assert capital.db.execute('SELECT count(*) FROM capital_accounts').fetchone()[0] == 0
    finally:
        capital.close()
        controller.store.close()
        adapter.store.close()


def test_open_rechecks_deployment_at_dispatch_but_allows_owned_cleanup(tmp_path):
    from factorlab.trading.portfolio_execution import PortfolioStore
    from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
    from test_binance_demo_execution import setup, NOW, intent
    import pytest
    adapter, gateway, controller, _ = setup()
    capital = PortfolioStore(tmp_path / 'dispatch-capital.sqlite3')
    session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller,
                              _now=lambda: NOW, instance_owner=None)
    calls = []
    owner = DemoInstanceOwner.open(session, capital, capability=lambda *args:
        (object(), lambda method, params, plan: calls.append(method)))
    binding = dict(account_id=adapter.account_id, instance_id='one', allocation_id='funded')
    controller.store.record_metadata('demo:instance:one', dict(binding=binding))
    callback = None
    view = SimpleNamespace(binding=lambda: binding, pending=lambda: None,
        activation_callback=lambda: callback, snapshot=lambda: dict(active='package'))
    runtime = SimpleNamespace(store=view, adapter=owner.execution, package_id='package')
    owner.attach(runtime, SimpleNamespace(store=view))
    try:
        authorization, guard = owner.execution.executor.adapter.capability(
            intent('original'), ('one', 'funded', 'NVDAUSDT'))
        callback = dict(package_id='package')
        with pytest.raises(SafetyBlocked, match='not active'):
            guard('POST', {}, authorization)
        assert calls == []
        guard('DELETE', {}, authorization)
        assert calls == ['DELETE']
        callback = None
        guard('POST', {}, authorization)
        assert calls == ['DELETE', 'POST']
        from dataclasses import replace
        from factorlab.trading import Side
        reduction = replace(intent('switch-exit'), side=Side.SELL, reduce_only=True)
        _, reduction_guard = owner.execution.executor.adapter.capability(
            reduction, ('one', 'funded', 'NVDAUSDT'))
        view.pending = lambda: dict(id=1)
        with pytest.raises(SafetyBlocked, match='not active'):
            reduction_guard('POST', {}, authorization)
        controller.store.record_metadata('demo:deployment-reduction:switch-exit',
            dict(binding=binding, switch_id=1))
        reduction_guard('POST', {}, authorization)
        assert calls == ['DELETE', 'POST', 'POST']
        view.pending = lambda: dict(id=2)
        with pytest.raises(SafetyBlocked, match='not active'):
            reduction_guard('POST', {}, authorization)
    finally:
        capital.close()
        controller.store.close()
        adapter.store.close()


def test_unavailable_symbol_does_not_block_other_instance(tmp_path):
    from factorlab.trading.storage import PaperStore, dumps
    from dataclasses import replace
    from test_binance_demo_execution import intent
    store = PaperStore(tmp_path / 'owner.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    calls = []
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='account'))
    execution = SimpleNamespace(session=session, store=store,
        advance=lambda decision_id, **kwargs: calls.append(decision_id) or 'WAITING')
    owner = DemoInstanceOwner(session, execution)
    try:
        for decision_id, symbol in [('unavailable', 'BTCUSDT'), ('healthy', 'ETHUSDT')]:
            store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)',
                (decision_id, dumps(dict(intents=[replace(intent(decision_id), instrument_id=symbol)]))))
        assert owner.advance(equity=5000, quotes={'ETHUSDT': object()},
            rules={'ETHUSDT': object()}, now=None) == {'healthy': 'WAITING'}
        assert calls == ['healthy']
    finally:
        store.close()


def test_account_exchange_failure_is_not_treated_as_symbol_unavailability(tmp_path):
    from factorlab.trading.storage import PaperStore, dumps
    from factorlab.integrations.binance_futures_trading.protocol import ExchangeError
    from test_binance_demo_execution import intent
    import pytest
    store = PaperStore(tmp_path / 'account-failure.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)',
        ('pending', dumps(dict(receipt=dict(binding=dict(account_id='account', instance_id='one')),
                               intents=[intent('pending')]))))
    def unavailable(symbol):
        raise ExchangeError(-2015, 401, 'AUTHENTICATION')
    session = SimpleNamespace(symbol_context=unavailable, adapter=SimpleNamespace(account_id='account'))
    capital = PortfolioStore(tmp_path / 'account-failure-capital.sqlite3')
    execution = SimpleNamespace(session=session, store=store, executor=PortfolioExecutor(None, capital),
        advance=lambda *args, **kwargs: pytest.fail('account failure must block advancement'))
    owner = DemoInstanceOwner(session, execution)
    try:
        with pytest.raises(ExchangeError, match='AUTHENTICATION'):
            owner.poll()
    finally:
        capital.close()
        store.close()


def test_poll_blocks_pending_execution_until_instance_activation_recovers(tmp_path):
    from decimal import Decimal
    from factorlab.trading.storage import PaperStore, dumps
    from test_binance_demo_execution import NOW
    store = PaperStore(tmp_path / 'activation-outbox.sqlite3')
    store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
    calls = []
    risk_accounts = []
    session = SimpleNamespace(_now=lambda: NOW, adapter=SimpleNamespace(account_id='account',
        read_facts=lambda: SimpleNamespace(account_id='account', usdt=lambda: dict(
            walletBalance=Decimal(5000), unrealizedProfit=Decimal(0)))))
    capital = PortfolioStore(tmp_path / 'activation-capital.sqlite3')
    executor = PortfolioExecutor(None, capital)
    executor.enforce_portfolio_risk = risk_accounts.append
    execution = SimpleNamespace(session=session, store=store, executor=executor,
        advance=lambda decision_id, **kwargs: calls.append(decision_id) or 'WAITING')
    owner = DemoInstanceOwner(session, execution)
    callback = {'package_id': 'package'}
    for instance_id in ('blocked', 'healthy'):
        view = SimpleNamespace(
            binding=lambda instance_id=instance_id: dict(account_id='account', instance_id=instance_id),
            pending=lambda: None,
            activation_callback=lambda instance_id=instance_id: callback if instance_id == 'blocked' else None,
            snapshot=lambda: dict(active='package'))
        runtime = SimpleNamespace(store=view, adapter=execution, package_id='package', recover=lambda: None)
        deployment = SimpleNamespace(store=view, step=lambda: calls.append('activation-retry'))
        owner.attach(runtime, deployment)
        store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)',
            (instance_id, dumps(dict(receipt=dict(binding=view.binding()), intents=[]))))
    try:
        assert owner.poll() == {'healthy': 'WAITING'}
        assert calls == ['activation-retry', 'healthy']
        callback = None
        calls.clear()
        assert owner.poll() == {'blocked': 'WAITING', 'healthy': 'WAITING'}
        assert calls == ['blocked', 'healthy']
        assert risk_accounts == ['account', 'account']
    finally:
        capital.close()
        store.close()


def test_activation_callback_is_retried_before_any_new_decision(tmp_path):
    calls = []
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='account'))
    capital = PortfolioStore(tmp_path / 'callback-capital.sqlite3')
    execution = SimpleNamespace(session=session, executor=PortfolioExecutor(None, capital))
    view = SimpleNamespace(binding=lambda: dict(account_id='account', instance_id='one'),
        pending=lambda: None, activation_callback=lambda: dict(package_id='package'),
        snapshot=lambda: dict(active='package'))
    runtime = SimpleNamespace(store=view, adapter=execution, package_id='package',
        recover=lambda: calls.append('recover'), decide=lambda event: calls.append('decide'))
    deployment = SimpleNamespace(store=view, step=lambda: calls.append('step'))
    owner = DemoInstanceOwner(session, execution)
    owner.attach(runtime, deployment)
    event = dict(binding=dict(instance_id='one'))
    try:
        assert owner.decide('one', event) is None
        assert calls == ['step']
        view.activation_callback = lambda: None
        owner.decide('one', event)
        assert calls == ['step', 'recover', 'decide']
    finally:
        capital.close()
