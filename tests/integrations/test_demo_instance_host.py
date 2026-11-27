from contextlib import ExitStack, closing
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_instance_host import DemoInstanceHost
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.strategies.loader import StrategyPackageError
from factorlab.trading import Market
from factorlab.trading.portfolio_execution import PortfolioStore
from test_binance_demo_execution import setup, NOW


def test_host_restores_registered_instances_from_existing_capital_without_allocating(tmp_path, monkeypatch):
    adapter, gateway, controller, _ = setup()
    binding = dict(market=Market.BINANCE_USDM.value, account_id=adapter.account_id,
        instance_id='one', allocation_id='original-allocation', strategy_id='grid')
    with closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')) as capital:
        capital.db.execute('INSERT INTO portfolio_allocations VALUES(?,?,?,?,?)',
                          (adapter.account_id, 'one', 'original-allocation', '7', '7'))
    loads, prepared = [], []
    def load(path, *, manifest, trusted):
        from test_strategy_protocol_v2 import spec
        assert trusted is True
        loads.append(path.name)
        if path.name == 'missing-package':
            raise StrategyPackageError('offline fixture: missing installed package')
        return SimpleNamespace(binding=dict(package_id=path.name), spec=spec(
            market=Market.BINANCE_USDM.value, account_id=adapter.account_id, symbols=['NVDAUSDT']))
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_instance_host.LoadedStrategy',
                        SimpleNamespace(from_strategy_package=load))
    def decision_runtime(strategy, store, execution, *, allocation):
        prepared.append(allocation)
        return SimpleNamespace(store=store, adapter=execution,
            package_id=strategy.binding['package_id'], recover=lambda: None)
    monkeypatch.setattr('factorlab.integrations.binance_futures_trading.demo_instance_host.InstanceDecisionRuntime',
                        decision_runtime)
    rows = [dict(instance_id='missing', binding=dict(binding, instance_id='missing')),
        dict(instance_id='one', binding=binding), dict(instance_id='other',
        binding=dict(binding, market='ALPACA_US_EQUITY'))]
    view = SimpleNamespace(legacy=False, instance_id='one', binding=lambda: binding,
        snapshot=lambda: dict(active='original-package'), pending=lambda: None,
        activation_callback=lambda: None, manifest=lambda version: dict(package_id=version), close=lambda: None)
    missing = SimpleNamespace(legacy=False, instance_id='missing',
        snapshot=lambda: dict(active='missing-package', previous=None), manifest=lambda version: {},
        close=lambda: None)
    registry = SimpleNamespace(instances=lambda: rows,
        for_instance=lambda instance: missing if instance == 'missing' else view)
    try:
        with closing(DemoRuntime(tmp_path)) as runtime:
            for _ in range(2):
                session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller,
                    _now=lambda: NOW, instance_owner=None)
                with ExitStack() as stack:
                    host = DemoInstanceHost(session, runtime, registry, tmp_path / 'releases',
                        tmp_path, stack, SimpleNamespace(desired=lambda: 'PAUSED'))
                    assert set(host.owner.instances) == {'one'}
                    assert host.instance_status['missing']['state'] == 'PACKAGE_UNAVAILABLE'
                    assert host.owner.instances['one'][0].package_id == 'original-package'
                    assert host.owner.execution.executor.store.allocation(adapter.account_id, 'one')['capital'] == D(7)
                    assert host.owner.execution.executor.store.db.execute('SELECT count(*) FROM portfolio_allocations').fetchone()[0] == 1
                    assert host.owner.execution.executor.store.db.execute('SELECT count(*) FROM capital_accounts').fetchone()[0] == 0
                    with pytest.raises(SafetyBlocked, match='not running'):
                        host.dispatch_gate(binding)
                    assert controller.limits.account_budget == D(7)
            assert loads == ['missing-package', 'original-package'] * 2
            assert all(allocation['allocation_id'] == 'original-allocation' for allocation in prepared)
    finally:
        controller.store.close()
        adapter.store.close()


@pytest.mark.parametrize('interrupted_activation', [False, True])
def test_host_loads_real_v2_package_and_recovers_its_original_decision(tmp_path, interrupted_activation):
    from dataclasses import asdict
    from factorlab.strategies.package import build_package, install_package
    from factorlab.strategies.deployment import DeploymentStore, DeploymentController, OfflineAdapter
    from factorlab.strategies.loader import LoadedStrategy
    from factorlab.strategies.protocol import V2
    from test_strategy_protocol_v2 import spec
    adapter, gateway, controller, _ = setup()
    declaration = spec('target_quantities', market=Market.BINANCE_USDM.value,
        account_id=adapter.account_id, symbols=['NVDAUSDT'],
        capital=dict(currency='USDT', requested=D(7)),
        data=dict(data_types=['closed_bar', 'order_event'], history_limit=3, historical_observations=False),
        execution=dict(order_types=['LIMIT'], time_in_force=['GTC'], shorting=False,
                       fractional_quantities=True, reduce_only=True))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'strategy.py').write_text(
        'from decimal import Decimal\nfrom factorlab.strategies.protocol import StrategySpecV2, DecisionV2\n'
        'class Strategy:\n'
        '    def describe(self):\n        return StrategySpecV2(**' + repr(asdict(declaration)) + ')\n'
        '    def decide(self, event, state):\n        return DecisionV2("target_quantities", targets={"NVDAUSDT": Decimal(0)}, state={"count": state.get("count", 0)+1})\n'
        '    def migrate(self, state, from_version):\n        return state\n', encoding='utf-8')
    data = asdict(declaration)
    requirements = {key: data[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital'] = dict(requirements['capital'], requested='7')
    manifest = dict(schema_version=2, interface=V2, strategy_id=declaration.strategy_id,
        strategy_version='2', market=declaration.market, account_id=declaration.account_id,
        symbols=declaration.symbols, execution_mode='target_quantities', requirements=requirements,
        code_commit='a'*40, entrypoint='strategy:Strategy', files={'strategy.py': ''}, configuration={},
        dependencies=[], evidence={'fixture': 'demo-startup'}, migration='wait_flat')
    archive = tmp_path / 'strategy.zip'
    manifest = build_package(source, manifest, archive)
    release, _ = install_package(archive, tmp_path / 'releases')
    binding = dict(market=declaration.market, account_id=adapter.account_id, instance_id='one',
                   allocation_id='original-allocation', strategy_id=declaration.strategy_id)
    try:
        with ExitStack() as outer:
            capital = outer.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
            capital.db.execute('INSERT INTO portfolio_allocations VALUES(?,?,?,?,?)',
                              (adapter.account_id, 'one', 'original-allocation', '7', '7'))
            capital.capital_policy.configure(account_id=adapter.account_id, currency='USDT', budget=D(27), fee_rate=D(0))
            registry = outer.enter_context(closing(DeploymentStore(tmp_path / 'deployment.sqlite3')))
            registry.register_instance(binding)
            view = outer.enter_context(closing(registry.for_instance('one')))
            view.request(manifest, dict(schema='FACTORLAB_CHAMPION_PROMOTION_V2', binding=binding,
                selection_id='original-selection', decision='PROMOTE', qualified=True, strictly_better=True,
                expected_active=None, package_id=manifest['package_id'], evidence=manifest['evidence']))
            loaded = LoadedStrategy.from_strategy_package(release, trusted=True)
            activation = OfflineAdapter()
            if interrupted_activation:
                def interrupted(version):
                    raise OSError('activation interrupted after durable switch')
                activation.activated = interrupted
            DeploymentController(view, activation, load_strategy=lambda version: loaded).step()
            assert (view.activation_callback() is not None) == interrupted_activation
            runtime = outer.enter_context(closing(DemoRuntime(tmp_path)))
            event = dict(protocol=V2, binding=binding, event_id='original-bar', event_time=NOW,
                data_type='closed_bar', symbols={'NVDAUSDT': dict(watermark='original-bar',
                    observations={}, history={})}, account={}, allocation={})
            for turn in range(2):
                session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller,
                    _now=lambda: NOW, instance_owner=None, status=lambda: {},
                    symbol_context=lambda symbol: SimpleNamespace(rules=gateway.rules),
                    _fresh_book=lambda **kwargs: dict(askPrice=D(100), bidPrice=D(100)),
                    limit_price=lambda *args: D(100))
                with ExitStack() as stack:
                    host = DemoInstanceHost(session, runtime, registry, tmp_path / 'releases',
                        tmp_path, stack, SimpleNamespace(desired=lambda: 'RUNNING'))
                    if interrupted_activation and view.activation_callback() is not None:
                        assert host.owner.decide('one', event) is None
                        assert view.activation_callback() is None
                        assert controller.store.db.execute('SELECT count(*) FROM demo_instance_outbox').fetchone()[0] == 0
                    receipt = host.deliver('one', event)
                    assert receipt['decision']['state'] == {'count': 1}
                    assert host.deliver('one', dict(event, account={'changed_after_restart': True})) == receipt
                    from factorlab.trading.realtime import PersistenceConflict
                    with pytest.raises(PersistenceConflict, match='market input identity'):
                        host.deliver('one', dict(event, symbols={'NVDAUSDT': dict(
                            watermark='changed', observations={}, history={})}))
                    assert controller.store.db.execute('SELECT count(*) FROM demo_instance_outbox').fetchone()[0] == 1 + turn
                    if turn == 0:
                        from test_binance_demo_execution import intent
                        from factorlab.trading import OrderEvent, OrderStatus
                        order = intent('original-instance-order')
                        host.owner.execution.owners.claim(order, instance_id='one', allocation_id='original-allocation')
                        controller.store.record_event(OrderEvent(order.client_order_id, None, OrderStatus.REJECTED,
                            D(0), None, NOW, 'OFFICIAL_DEMO', 'original-instance-confirmation'))
                        host.collect_order_events()
                        assert view.snapshot()['checkpoint'] == {'count': 1}
                        assert view.snapshot()['active'] == manifest['package_id']
                        host.owner.advance(equity=D(5000), quotes={}, rules={}, now=NOW)
                    host.collect_order_events()
                    assert view.snapshot()['checkpoint'] == {'count': 2}
                    assert controller.store.db.execute('SELECT count(*) FROM demo_instance_outbox').fetchone()[0] == 2
                    assert capital.db.execute('SELECT count(*) FROM portfolio_allocations').fetchone()[0] == 1
    finally:
        controller.store.close()
        adapter.store.close()


def test_paused_host_does_not_advance_unattached_deployment(tmp_path):
    from unittest.mock import Mock
    adapter, gateway, controller, _ = setup()
    try:
        with ExitStack() as stack:
            stack.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
            runtime = stack.enter_context(closing(DemoRuntime(tmp_path)))
            control = SimpleNamespace(desired=lambda: 'PAUSED')
            host = DemoInstanceHost(SimpleNamespace(adapter=adapter, gateway=gateway,
                controller=controller, _now=lambda: NOW, instance_owner=None), runtime,
                SimpleNamespace(instances=lambda: []), tmp_path / 'releases', tmp_path, stack, control)
            deployment = Mock()
            host.controllers['pending'] = deployment
            host.refresh = Mock()
            host.step()
            host.refresh.assert_not_called()
            deployment.step.assert_not_called()
            control.desired = lambda: 'RUNNING'
            host.step()
            host.refresh.assert_called_once_with()
            deployment.step.assert_called_once_with()
    finally:
        controller.store.close()
        adapter.store.close()


def test_host_waits_for_matching_portfolio_bars_and_preserves_history(tmp_path):
    from datetime import timedelta
    from unittest.mock import Mock
    from dataclasses import replace
    from factorlab.trading.strategy import ClosedBar
    from factorlab.integrations.binance_futures_trading.demo_market_feed import SOURCE
    adapter, gateway, controller, _ = setup()
    try:
        with ExitStack() as stack:
            stack.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
            runtime = stack.enter_context(closing(DemoRuntime(tmp_path)))
            host = DemoInstanceHost(SimpleNamespace(adapter=adapter, gateway=gateway,
                controller=controller, _now=lambda: NOW, instance_owner=None, status=lambda: {}),
                runtime, SimpleNamespace(instances=lambda: []), tmp_path / 'releases', tmp_path,
                stack, SimpleNamespace(desired=lambda: 'RUNNING'))
            spec = SimpleNamespace(symbols=['BTCUSDT', 'ETHUSDT'], decision_kind='target_weights',
                data=dict(data_types=['closed_bar', 'funding_settlement'], history_limit=3),
                schedule=dict(kind='interval', interval_seconds=60))
            binding = dict(account_id=adapter.account_id, instance_id='portfolio')
            host.owner.instances['portfolio'] = (SimpleNamespace(package_id='original-package', strategy=SimpleNamespace(spec=spec, binding={}),
                store=SimpleNamespace(binding=lambda: binding)), None)
            host.deliver = Mock(return_value='accepted')
            bar = ClosedBar('BTCUSDT', NOW-timedelta(minutes=1), NOW,
                D(100), D(100), D(100), D(100), D(1), 60, SOURCE)
            warmup = replace(bar, opened_at=bar.opened_at-timedelta(minutes=1),
                             closed_at=bar.closed_at-timedelta(minutes=1))
            for symbol in spec.symbols:
                assert host.closed_bar(replace(warmup, symbol=symbol), historical=True) == {}
            # Even a fresh historical callback must not become a trading event.
            assert host.closed_bar(bar, historical=True) == {}
            host.context.observe('ETHUSDT', 'funding_settlement', event_id='original-funding',
                observed_at=NOW-timedelta(seconds=10), effective_at=NOW-timedelta(minutes=30),
                source=SOURCE, payload=dict(funding_rate=D('.0001'), mark_price=D(100)))
            assert host.closed_bar(bar) == {}
            host.deliver.assert_not_called()
            assert host.closed_bar(replace(bar, symbol='ETHUSDT')) == {'portfolio': 'accepted'}
            event = host.deliver.call_args.args[1]
            assert set(event['symbols']) == {'BTCUSDT', 'ETHUSDT'}
            assert all(context['observations']['closed_bar']['effective_at'] == NOW
                       for context in event['symbols'].values())
            assert 'funding_settlement' not in event['symbols']['BTCUSDT']['history']
            assert event['symbols']['ETHUSDT']['observations']['funding_settlement']['payload']['funding_rate'] == D('.0001')
            assert len(event['symbols']['BTCUSDT']['history']['closed_bar']) == 2
            assert event['symbols']['BTCUSDT']['history']['closed_bar'][0]['payload']['close'] == D(100)
            from factorlab.trading.storage import dumps
            host.owner.execution.store.db.execute('INSERT INTO demo_instance_inputs VALUES(?,?,?,?)',
                ('portfolio', 'original-package', event['event_id'], dumps(event)))
            host.context.observe('ETHUSDT', 'funding_settlement', event_id='late-old-funding',
                observed_at=NOW, effective_at=NOW-timedelta(hours=1),
                source=SOURCE, payload=dict(funding_rate=D('.0002'), mark_price=D(100)))
            sources = SimpleNamespace(bars=Mock(return_value=None), funding=Mock(return_value=None))
            host.owner.market_sources = sources
            host.collect_funding()
            sources.funding.assert_any_call('ETHUSDT',
                after=round((NOW-timedelta(minutes=30)).timestamp()*1000)+1)
            sources.funding.assert_any_call('BTCUSDT', after=None)
            host.deliver.reset_mock()
            host.collect_bars()
            assert host.deliver.call_count == 2
            assert all(call.args[1] == event for call in host.deliver.call_args_list)
            assert all(call.kwargs['after'] == round(NOW.timestamp()*1000)
                       for call in sources.bars.call_args_list)
            from factorlab.trading.realtime import PersistenceConflict
            with pytest.raises(PersistenceConflict, match='closed bar identity'):
                host.closed_bar(replace(bar, close=D(99), low=D(99)))
    finally:
        controller.store.close()
        adapter.store.close()


def test_host_routes_original_confirmations_and_resumes_blocked_delivery(tmp_path):
    from unittest.mock import Mock
    from test_binance_demo_execution import intent
    from factorlab.trading import OrderEvent, OrderStatus
    from factorlab.trading.storage import dumps
    adapter, gateway, controller, _ = setup()
    try:
        with ExitStack() as stack:
            stack.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
            runtime = stack.enter_context(closing(DemoRuntime(tmp_path)))
            control = SimpleNamespace(desired=lambda: 'RUNNING')
            host = DemoInstanceHost(SimpleNamespace(adapter=adapter, gateway=gateway,
                controller=controller, _now=lambda: NOW, instance_owner=None, status=lambda: {}),
                runtime, SimpleNamespace(instances=lambda: []), tmp_path / 'releases', tmp_path,
                stack, control)
            for name in ('one', 'two'):
                binding = dict(account_id=adapter.account_id, instance_id=name, allocation_id=name)
                host.owner.instances[name] = (SimpleNamespace(package_id='package',
                    strategy=SimpleNamespace(spec=SimpleNamespace(decision_kind='target_quantities', data={'data_types': ['order_event']})),
                    store=SimpleNamespace(binding=lambda binding=binding: binding)), None)
                order = intent('original-' + name)
                host.owner.execution.owners.claim(order, instance_id=name, allocation_id=name)
                controller.store.record_event(OrderEvent(order.client_order_id, None, OrderStatus.FILLED,
                    order.quantity, D(100), NOW, 'OFFICIAL_DEMO', 'confirmation-' + name))
            host.deliver = Mock(side_effect=lambda instance, event: None if instance == 'one' else 'accepted')
            host.collect_order_events()
            assert [call.args[0] for call in host.deliver.call_args_list] == ['one', 'two']
            first = host.deliver.call_args_list[0].args[1]
            assert first['symbols']['NVDAUSDT']['observations']['order_event']['payload']['client_order_id'] == 'original-one'
            controller.store.db.execute('INSERT INTO demo_instance_inputs VALUES(?,?,?,?)',
                ('one', 'package', first['event_id'], dumps(first)))
            host.session._now = lambda: NOW.replace(second=1)
            host.deliver = Mock(return_value='accepted')
            host.collect_order_events()
            host.deliver.assert_called_once_with('one', first)
            host.deliver.reset_mock()
            host.collect_order_events()
            host.deliver.assert_not_called()
            control.desired = lambda: 'PAUSED'
            host.collect_order_events()
            host.deliver.assert_not_called()
    finally:
        controller.store.close()
        adapter.store.close()


def test_host_does_not_create_missing_capital_journal(tmp_path):
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='account'),
        controller=SimpleNamespace(store=SimpleNamespace(event_payload=lambda key: None)))
    with ExitStack() as stack:
        with pytest.raises(SafetyBlocked, match='existing account portfolio'):
            DemoInstanceHost(session, None, None, tmp_path / 'releases', tmp_path, stack, None)
    assert not (tmp_path / 'portfolio.sqlite3').exists()
