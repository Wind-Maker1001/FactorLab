from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import setup, intent, NOW
from factorlab.integrations.binance_futures_trading.demo_legacy_migration import export_sleeve
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


def test_account_migration_exports_all_sleeves_before_atomic_adoption(monkeypatch, tmp_path):
    from unittest.mock import Mock
    from factorlab.integrations.binance_futures_trading import demo_legacy_migration as migration
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='original-account',
        read_facts=lambda: SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(-2)))))
    from factorlab.trading.storage import PaperStore
    journal = PaperStore(tmp_path / 'migration.sqlite3')
    session.controller = SimpleNamespace(store=journal, broker=SimpleNamespace())
    executor = SimpleNamespace(adapter=SimpleNamespace(session=session), adopt_existing=Mock())
    sleeves = [dict(kind=kind, instance_id=kind, logical_strategy=kind, allocation_id=kind,
        capital=D(amount), initial_cash=D(amount), initial_inventory={})
        for kind, amount in [('grid', 7), ('portfolio', 20)]]
    manifest = dict(account_id='original-account', history_complete=True, allocations={sleeve['allocation_id']: dict(sleeve)
                                                     for sleeve in sleeves})
    monkeypatch.setattr(migration, 'export_grid_sleeve', lambda session, **values: dict(values, orders=[]))
    def missing(session, **values):
        raise SafetyBlocked('missing original portfolio history')
    monkeypatch.setattr(migration, 'export_portfolio_sleeve', missing)
    with pytest.raises(SafetyBlocked, match='missing original portfolio'):
        migration.adopt_account_history(session, executor, receipt_id='original', manifest=manifest, sleeves=sleeves)
    executor.adopt_existing.assert_not_called()
    monkeypatch.setattr(migration, 'export_portfolio_sleeve', lambda session, **values: dict(values, orders=[]))
    migration.adopt_account_history(session, executor, receipt_id='original', manifest=manifest, sleeves=sleeves)
    call = executor.adopt_existing.call_args.kwargs
    assert call['official_equity'] == D(4998)
    assert [value['initial_cash'] for value in call['sleeves']] == [D(7), D(20)]
    assert call['receipt_id'] == 'original'
    frozen = call['sleeves']
    journal.close()
    session.controller.store = PaperStore(tmp_path / 'migration.sqlite3')
    monkeypatch.setattr(migration, 'export_grid_sleeve', Mock(side_effect=AssertionError('must reuse frozen export')))
    monkeypatch.setattr(migration, 'export_portfolio_sleeve', Mock(side_effect=AssertionError('must reuse frozen export')))
    executor.adopt_existing.side_effect = OSError('interrupted adoption')
    with pytest.raises(OSError, match='interrupted adoption'):
        migration.adopt_account_history(session, executor, receipt_id='original', manifest=manifest, sleeves=sleeves)
    executor.adopt_existing.side_effect = None
    migration.resume_account_history(session, executor)
    assert executor.adopt_existing.call_args.kwargs['sleeves'] == frozen
    with pytest.raises(SafetyBlocked, match='source identity changed'):
        migration.adopt_account_history(session, executor, receipt_id='original',
            manifest=dict(manifest, evidence='changed'), sleeves=sleeves)
    executor.adopt_existing.reset_mock()
    with pytest.raises(SafetyBlocked, match='complete original account'):
        migration.adopt_account_history(session, executor, receipt_id='original', manifest=manifest, sleeves=sleeves[:1])
    executor.adopt_existing.assert_not_called()
    with pytest.raises(SafetyBlocked, match='original account execution owner'):
        migration.adopt_account_history(session, executor, receipt_id='original',
            manifest=dict(manifest, account_id='foreign-account'), sleeves=sleeves)
    executor.adopt_existing.assert_not_called()
    session.controller.store.close()


def test_complete_original_source_starts_initial_migration_and_reuses_it(monkeypatch, tmp_path):
    from unittest.mock import Mock
    from contextlib import closing
    from factorlab.trading.storage import PaperStore
    from factorlab.integrations.binance_futures_trading import demo_legacy_migration as migration
    with closing(PaperStore(tmp_path / 'source.sqlite3')) as journal:
        session = SimpleNamespace(adapter=SimpleNamespace(account_id='original-account',
            read_facts=lambda: SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))),
            controller=SimpleNamespace(store=journal, broker=SimpleNamespace()))
        sleeves = [dict(kind=kind, instance_id=kind, logical_strategy=kind, allocation_id=kind,
            capital=D(amount), initial_cash=D(amount), initial_inventory={})
            for kind, amount in [('grid', 7), ('portfolio', 20)]]
        manifest = dict(account_id='original-account', history_complete=True,
            allocations={value['allocation_id']: dict(value) for value in sleeves})
        monkeypatch.setattr(migration, 'export_grid_sleeve', lambda session, **values: dict(values, orders=[]))
        monkeypatch.setattr(migration, 'export_portfolio_sleeve', lambda session, **values: dict(values, orders=[]))
        migration.register_account_history_source(session, receipt_id='original', manifest=manifest,
            sleeves=sleeves, fee_rate=D('.0003'))
        executor = SimpleNamespace(adapter=SimpleNamespace(session=session), capital_policy=Mock(), adopt_existing=Mock())
        migration.start_account_history(session, executor)
        executor.capital_policy.configure.assert_called_once_with(account_id='original-account',
            currency='USDT', budget=D(27), fee_rate=D('.0003'))
        assert [value['capital'] for value in executor.adopt_existing.call_args.kwargs['sleeves']] == [D(7), D(20)]
        frozen = executor.adopt_existing.call_args.kwargs['sleeves']
        monkeypatch.setattr(migration, 'export_grid_sleeve', Mock(side_effect=AssertionError('frozen')))
        migration.start_account_history(session, executor)
        assert executor.adopt_existing.call_args.kwargs['sleeves'] == frozen
        with pytest.raises(SafetyBlocked, match='original 7/20'):
            migration.register_account_history_source(session, receipt_id='larger', manifest=manifest,
                sleeves=[dict(sleeves[0], capital=D(8)), sleeves[1]], fee_rate=D('.0003'))


def test_history_import_cli_reconciles_without_authorizing_writes(monkeypatch, tmp_path, request):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from factorlab.trading.storage import dumps
    from factorlab.integrations.binance_futures_trading import demo_cli, demo_legacy_migration as migration
    from factorlab.trading import portfolio_execution
    from factorlab.trading.storage import PaperStore
    journal = PaperStore(tmp_path / 'original.sqlite3')
    request.addfinalizer(journal.close)
    session = SimpleNamespace(adapter=SimpleNamespace(account_id='original-account'), _now=lambda: NOW,
        controller=SimpleNamespace(store=journal, broker=SimpleNamespace()))
    @contextmanager
    def opened(*args):
        yield session, None
    monkeypatch.setattr(demo_cli, 'open_session', opened)
    recovered = Mock()
    registered = Mock()
    monkeypatch.setattr(demo_cli, 'recover', recovered)
    monkeypatch.setattr(migration, 'register_account_history_source', registered)
    def executor(adapter, store, **kwargs):
        with pytest.raises(SafetyBlocked, match='official reads only'):
            adapter.capability(None)
        return SimpleNamespace(adapter=adapter)
    monkeypatch.setattr(portfolio_execution, 'PortfolioExecutor', executor)
    started = Mock()
    monkeypatch.setattr(migration, 'start_account_history', started)
    source = dict(receipt_id='original-history', fee_rate=D('.0003'))
    path = tmp_path / 'source.json'
    path.write_text(dumps(source), encoding='utf-8')
    directory = tmp_path / demo_cli.journal_id('original-account')
    directory.mkdir()
    result = demo_cli.run_command(SimpleNamespace(command='capital-history-import', source=str(path)),
        root=tmp_path, store=None, runtime=None, requests=None, smoke_id=None)
    assert result == dict(status='DEMO_CAPITAL_HISTORY_ADOPTED', receipt_id='original-history')
    recovered.assert_called_once_with(session)
    registered.assert_called_once_with(session, **source)
    started.assert_called_once()


def test_export_preserves_original_events_and_actual_notional_without_cash_plug():
    adapter, gateway, controller, _ = setup()
    session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller)
    try:
        controller.submit(intent('legacy-original', limit='100', quantity='.1'))
        gateway.recover(controller)
        supplied = dict(instance_id='legacy', logical_strategy='grid', allocation_id='grid-seven',
            capital=D(7), initial_cash=D(7), initial_inventory={}, owned_symbols=['NVDAUSDT'],
            created_at=NOW, trading_session='original-session', order_ids=['legacy-original'],
            source_receipt=dict(history_complete=True, deployment='original-deployment',
                order_ids=['legacy-original'], event_ids=[event.event_id for _, event in
                    controller.store.order_events() if event.client_order_id == 'legacy-original']))
        for altered in (dict(order_ids=[]), dict(event_ids=[]),
                        dict(event_ids=supplied['source_receipt']['event_ids']+['missing-confirmation'])):
            with pytest.raises(SafetyBlocked, match='identity manifest'):
                export_sleeve(session, **dict(supplied,
                    source_receipt=dict(supplied['source_receipt'], **altered)))
        exported = export_sleeve(session, **supplied)
        from factorlab.trading.portfolio_runtime import PortfolioRuntime
        from factorlab.integrations.binance_futures_trading.demo_legacy_migration import export_portfolio_sleeve
        portfolio = PortfolioRuntime(controller, allocation_id='grid-seven',
            owned_symbols=['NVDAUSDT'], recover=lambda: gateway.read_account())
        portfolio.begin('original-task', weights={'NVDAUSDT': D('.5')}, equity=D(7))
        task_key = portfolio._key('original-task')
        controller.store.record_metadata(task_key + ':batch:0', [intent('legacy-original', limit='100', quantity='.1')])
        arguments = {key: value for key, value in supplied.items() if key not in {'order_ids', 'source_receipt'}}
        receipt = dict(supplied['source_receipt'], task_journal_ids=[task_key])
        assert export_portfolio_sleeve(session, **arguments, source_receipt=receipt)['orders'] == exported['orders']
        with pytest.raises(SafetyBlocked, match='task manifest'):
            export_portfolio_sleeve(session, **arguments,
                source_receipt=dict(receipt, task_journal_ids=[]))
        assert exported['initial_cash'] == D(7)
        assert exported['orders'][0]['record'].snapshot.intent.client_order_id == 'legacy-original'
        assert exported['orders'][0]['filled_notional'] == adapter._load('legacy-original')[1]
        assert exported['source_receipt']['event_ids'] == [event.event_id for _, event in
            controller.store.order_events() if event.client_order_id == 'legacy-original']
        assert exported['source_receipt']['funding_coverage']['complete']
        from factorlab.integrations.binance_futures_trading.demo_portfolio_adapter import DemoPortfolioAdapter
        from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
        destination = PortfolioStore(':memory:')
        executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=lambda *args:
            pytest.fail('migration must not submit')), destination, quote_currency='USDT', clock=lambda: NOW)
        executor.capital_policy.configure(account_id=adapter.account_id, currency='USDT',
            budget=D(27), fee_rate=D('.001'))
        try:
            migration = dict(receipt_id='original-demo-migration', account_id=adapter.account_id,
                sleeves=[exported], official_equity=D(1000))
            from factorlab.integrations.binance_futures_trading.demo_legacy_migration import adopt_account_history
            account_manifest = dict(account_id=adapter.account_id, history_complete=True,
                allocations={'grid-seven': {field: supplied[field] for field in
                    ('instance_id', 'logical_strategy', 'capital', 'initial_cash', 'initial_inventory')}})
            deployment_key = 'demo-grid:original-deployment:deployment'
            controller.store.record_metadata(deployment_key, dict(account_id=adapter.account_id,
                instrument_id='NVDAUSDT', deployment_id='original-deployment'))
            for index, event in enumerate(exported['events']):
                controller.store.record_metadata('demo-grid:original-deployment:command:' + str(index),
                    dict(action='confirm', value=event))
            sleeve = dict(supplied, kind='grid', source_receipt=dict(supplied['source_receipt'],
                deployment_journal_ids=[deployment_key]))
            from factorlab.integrations.binance_futures_trading.demo_legacy_migration import export_grid_sleeve
            with pytest.raises(SafetyBlocked, match='deployment manifest'):
                export_grid_sleeve(session, **dict(supplied, source_receipt=dict(
                    supplied['source_receipt'], deployment_journal_ids=[])))
            adopt_account_history(session, executor, receipt_id=migration['receipt_id'],
                                  manifest=account_manifest, sleeves=[sleeve])
            adopt_account_history(session, executor, receipt_id=migration['receipt_id'],
                                  manifest=account_manifest, sleeves=[sleeve])
            assert destination.allocation(adapter.account_id, 'legacy')['initial_cash'] == D(7)
            assert destination.db.execute('SELECT id FROM portfolio_orders').fetchall() == [('legacy-original',)]
            from factorlab.integrations.binance_futures_trading.demo_allocations import DemoAllocations
            assert DemoAllocations(controller).owner('legacy-original') == ('legacy', 'grid-seven', 'NVDAUSDT')
            assert destination.holdings(adapter.account_id) == {
                'NVDAUSDT': gateway.state.positions.get('NVDAUSDT').quantity
                if 'NVDAUSDT' in gateway.state.positions else D(0)}
        finally:
            destination.close()
        with pytest.raises(SafetyBlocked, match='complete original allocation history'):
            export_sleeve(session, **dict(supplied, source_receipt={}))
    finally:
        controller.store.close()
        adapter.store.close()
