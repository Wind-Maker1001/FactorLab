from contextlib import ExitStack, closing
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_cli import open_session
from factorlab.integrations.binance_futures_trading.demo_fresh_capital import initialize, source
from factorlab.integrations.binance_futures_trading.demo_instance_host import DemoInstanceHost
from factorlab.integrations.binance_futures_trading.demo_portfolio_adapter import DemoPortfolioAdapter
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_binance_demo_network import wired
from test_binance_demo_strategy_session import deployment


def configuration():
    return dict(allocations=[dict(instance_id=name, allocation_id=name+'-capital',
        strategy_id='acceptance', capital=D(10)) for name in ('fresh-one', 'fresh-two')],
        symbols=['NVDAUSDT'], fee_rate=D('.001'))


def shared(session):
    session.controller.store.record_metadata('demo:static-shared-account:v1',
        dict(grid=['NVDAUSDT'], portfolio=['QQQUSDT','SPYUSDT'],
             grid_budget=D(7), portfolio_budget=D(20)))


def test_fresh_baseline_replay_restart_and_host_preserve_risk(deployment):
    root, credentials, campaign, _ = deployment
    with closing(DemoRuntime(root)) as runtime:
        original = None
        for restart in (False, True):
            with open_session(root, credentials, runtime, dict(GET=0,POST=0,DELETE=0),
                              campaign=campaign) as (session, _), ExitStack() as stack:
                shared(session)
                session.gateway.recover(session.controller)
                limits = session.controller.limits
                directory = root/'fresh'
                directory.mkdir(exist_ok=True)
                store = stack.enter_context(closing(PortfolioStore(directory/'portfolio.sqlite3')))
                executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=lambda *_: None),
                    store, quote_currency='USDT', clock=session._now)
                receipt = initialize(session, executor, **configuration())
                if not restart:
                    original = receipt
                assert receipt == original
                assert initialize(session, executor, **configuration()) == receipt
                assert store.allocated_capital(session.adapter.account_id) == D(20)
                assert store.db.execute('SELECT budget FROM capital_accounts').fetchone() == ('20',)
                assert store.db.execute('SELECT count(*) FROM portfolio_allocations').fetchone()[0] == 2
                host = DemoInstanceHost(session, runtime, SimpleNamespace(instances=lambda: []),
                    directory/'releases', directory, stack, SimpleNamespace(desired=lambda: 'PAUSED'))
                host.refresh()
                assert session.controller.limits == limits
                assert source(session)['historical_capital_migration'] == 'INCOMPLETE'
                assert session.controller.store.event_payload('demo:capital-migration:source') is None
                assert session.controller.store.event_payload('demo:static-shared-account:v1') is not None


def test_fresh_baseline_recovers_after_controller_receipt_interruption(deployment, monkeypatch):
    root, credentials, campaign, _ = deployment
    with closing(DemoRuntime(root)) as runtime, open_session(root, credentials, runtime,
            dict(GET=0,POST=0,DELETE=0), campaign=campaign) as (session,_), closing(PortfolioStore(root/'portfolio.sqlite3')) as store:
        shared(session)
        session.gateway.recover(session.controller)
        executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=lambda *_: None),
            store, quote_currency='USDT', clock=session._now)
        write = session.controller.store.record_metadata
        monkeypatch.setattr(session.controller.store, 'record_metadata',
            lambda *_: (_ for _ in ()).throw(OSError('interrupted mirror')))
        with pytest.raises(OSError, match='interrupted mirror'):
            initialize(session, executor, **configuration())
        assert store.allocated_capital(session.adapter.account_id) == D(20)
        monkeypatch.setattr(session.controller.store, 'record_metadata', write)
        receipt = source(session, store)
        session.instance_owner = None
        with ExitStack() as stack:
            DemoInstanceHost(session, runtime, SimpleNamespace(instances=lambda: []), root/'releases',
                root, stack, SimpleNamespace(desired=lambda: 'PAUSED'))
        assert receipt == source(session)
        assert initialize(session, executor, **configuration()) == receipt
        assert store.db.execute('SELECT count(*) FROM portfolio_allocations').fetchone()[0] == 2
        session.controller.limits = replace(session.controller.limits, account_budget=D(47))
        with pytest.raises(SafetyBlocked, match='risk limits changed'):
            initialize(session, executor, **configuration())


def test_fresh_baseline_rolls_back_partial_public_funding(deployment, monkeypatch):
    root, credentials, campaign, _ = deployment
    with closing(DemoRuntime(root)) as runtime, open_session(root, credentials, runtime,
            dict(GET=0,POST=0,DELETE=0), campaign=campaign) as (session,_), closing(PortfolioStore(root/'portfolio.sqlite3')) as store:
        shared(session)
        session.gateway.recover(session.controller)
        executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=lambda *_: None),
            store, quote_currency='USDT', clock=session._now)
        allocate = executor.allocate
        def interrupt(**kwargs):
            allocate(**kwargs)
            raise OSError('interrupted public registration')
        monkeypatch.setattr(executor, 'allocate', interrupt)
        with pytest.raises(OSError, match='interrupted public registration'):
            initialize(session, executor, **configuration())
        assert store.allocated_capital(session.adapter.account_id) == 0
        assert store.db.execute('SELECT count(*) FROM capital_accounts').fetchone()[0] == 0
        assert source(session, store) is None
        assert source(session) is None
        monkeypatch.setattr(executor, 'allocate', allocate)
        initialize(session, executor, **configuration())
        assert store.allocated_capital(session.adapter.account_id) == D(20)


@pytest.mark.parametrize('kind', ['amount','legacy','cash','unreconciled'])
def test_fresh_baseline_rejects_invalid_funding_without_postings(deployment, monkeypatch, kind):
    root, credentials, campaign, _ = deployment
    with closing(DemoRuntime(root)) as runtime, open_session(root, credentials, runtime,
            dict(GET=0,POST=0,DELETE=0), campaign=campaign) as (session,_), closing(PortfolioStore(root/'fresh.sqlite3')) as store:
        if kind != 'legacy':
            shared(session)
        session.gateway.recover(session.controller)
        config = configuration()
        if kind == 'amount':
            config['allocations'][0]['capital'] = D(11)
        if kind == 'cash':
            monkeypatch.setattr(session.adapter, 'read_facts', lambda: SimpleNamespace(usdt=lambda: dict(availableBalance=D(46))))
        if kind == 'unreconciled':
            account = session.gateway.read_account()
            monkeypatch.setattr(session.gateway, 'read_account', lambda: replace(account, reconciled=False))
        executor = PortfolioExecutor(DemoPortfolioAdapter(session, capability=lambda *_: None),
            store, quote_currency='USDT', clock=session._now)
        with pytest.raises(SafetyBlocked):
            initialize(session, executor, **config)
        assert store.allocated_capital(session.adapter.account_id) == 0
        assert source(session, store) is None
