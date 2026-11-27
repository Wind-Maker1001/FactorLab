"""Run the Observer-to-Execute mailbox against loopback Demo HTTP in separate venvs.

Run from the checkout root with PYTHONPATH=src:
    <execute-python> -m tools.ci.binance_mailbox_acceptance <observer-python>
"""
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import build_opener, ProxyHandler, Request

from tools.ci.demo_execute_systemd import OfflineDemoExchange, start_stub_server

from factorlab.integrations.binance_futures_trading.tradfi_tasks import PortfolioTask, TaskMailbox

START = datetime(2026, 11, 30, 14, 30, 2, tzinfo=UTC)
CONFIG = dict(revision='offline', equity_usdt='20', weights={'QQQUSDT': '.5', 'SPYUSDT': '.5'})


class MailboxExchange(OfflineDemoExchange):
    def __init__(self):
        super().__init__()
        self.now = START
        self.symbols = ('NVDAUSDT', 'QQQUSDT', 'SPYUSDT', 'BTCUSDT')

    def server_ms(self):
        return int(self.now.timestamp() * 1000)

    def handle(self, method, path, query):
        status, payload = super().handle(method, path, query)
        params = parse_qs(query)
        symbol = params.get('symbol', ['NVDAUSDT'])[0]
        if path == '/fapi/v1/exchangeInfo':
            payload['symbols'] = [dict(payload['symbols'][0], symbol=s, contractType=(
                'TRADIFI_PERPETUAL' if s in CONFIG['weights'] else 'PERPETUAL')) for s in self.symbols]
        elif path == '/fapi/v1/symbolConfig':
            payload = [dict(payload[0], symbol=s) for s in self.symbols]
        elif path == '/fapi/v1/leverageBracket':
            payload[0]['symbol'] = symbol
        elif path == '/fapi/v1/apiTradingStatus':
            payload['indicators'] = {s: payload['indicators']['NVDAUSDT'] for s in self.symbols}
        elif path in ('/fapi/v1/ticker/bookTicker', '/fapi/v1/premiumIndex'):
            payload['symbol'] = symbol
        elif path in ('/fapi/v1/userTrades', '/fapi/v1/allOrders'):
            payload = [row for row in payload if row['symbol'] == symbol]
        elif path == '/fapi/v3/account':
            for position in payload['positions']:
                position['symbol'] = 'BTCUSDT'
        elif path == '/fapi/v3/positionRisk':
            for position in payload:
                position['symbol'] = 'BTCUSDT'
        return status, payload

    def fill_btc(self, client_id):
        self.fill_order(client_id)
        row = self.orders[client_id]
        self.trades[-1].update(symbol='BTCUSDT', price=row['price'], side=row['side'],
                               time=row['updateTime'], realizedPnl='0', buyer=True, maker=False)


@contextmanager
def local_exchange(exchange):
    from factorlab.integrations.binance_futures_trading import demo_runtime, network, protocol
    from factorlab.integrations.binance_futures_trading.demo_session import DemoSession

    server = start_stub_server(exchange)

    def offline_opener(*handlers):
        opener = build_opener(ProxyHandler({}), *handlers)
        send = opener.open

        def open_request(request, timeout):
            url = urlsplit(request.full_url)
            assert url.scheme == 'https' and url.netloc == 'demo-fapi.binance.com'
            url = urlunsplit(('http', f'127.0.0.1:{server.server_port}', url.path, url.query, ''))
            return send(Request(url, headers=dict(request.header_items()), method=request.method), timeout=timeout)
        opener.open = open_request
        return opener

    try:
        with (patch.object(protocol, 'build_opener', offline_opener),
              patch.object(network, 'build_opener', offline_opener),
              patch.object(network, 'datetime', SimpleNamespace(now=lambda tz: exchange.now)),
              patch.object(demo_runtime, 'now', lambda: exchange.now),
              patch.object(DemoSession, '_now', lambda self: exchange.now)):
            yield
    finally:
        server.shutdown()
        server.server_close()


class DemoBinding:
    reference = 'synthetic-mailbox-acceptance'
    config = None

    def read(self):
        return self.credential

    def read_config(self):
        return self.config

    def write(self, credential):
        self.credential = credential

    def write_config(self, config):
        self.config = config


def produce(root):
    from factorlab.integrations.binance_futures_trading.tradfi_cloud import WeeklySchedule, dispatch, us_calendar
    import pandas as pd

    window = WeeklySchedule(us_calendar('2026-11-01', '2027-01-31')).next_window(START)
    quotes = {s: {'time': int(START.timestamp() * 1000)} for s in CONFIG['weights']}
    with closing(TaskMailbox(root / 'tradfi-tasks.sqlite3')) as box:
        scheduled = dispatch(box, CONFIG, window, quotes, START)
        acceptance = dispatch(box, CONFIG, window, quotes, START, acceptance_task_id='qqq-spy-acceptance')
        assert scheduled == dict(task_id='tradfi-qqq-spy-test:2026-11-30:offline', status='QUEUED')
        assert acceptance == dict(task_id='qqq-spy-acceptance', status='QUEUED')
        # Seed exact pre-fix bytes as produced by pandas, including a 1 ns lifetime.
        old = PortfolioTask('legacy-nanosecond', 'funded', window.session,
                            '2026-11-30T09:30:02-05:00', '2026-11-30T09:30:02.000000001-05:00',
                            D(10), {'BTCUSDT': D(0)})
        payload = dict(schema_version=1, task_id=old.task_id, allocation_id=old.allocation_id,
                       task_type=old.task_type, session=old.session, equity_usdt='10', weights={'BTCUSDT': '0'})
        for key in ('observed_at', 'valid_until'):
            payload[key] = pd.Timestamp(getattr(old, key)).tz_convert('UTC').isoformat()
        with box.db:
            box.db.execute('INSERT INTO portfolio_tasks VALUES (?, ?, ?)', (old.task_id,
                json.dumps(payload, sort_keys=True, separators=(',', ':')), 'QUEUED'))


def status(box, task_id):
    return next(row for row in box.snapshot() if row['task_id'] == task_id)


@contextmanager
def open_execute(root, credentials):
    from factorlab.integrations.binance_futures_trading.demo_cli import open_session
    from factorlab.integrations.binance_futures_trading.demo_instance_capability import DemoInstanceCapability
    from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
    from factorlab.integrations.binance_futures_trading.demo_portfolio_consumer import DemoPortfolioConsumer
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
    from factorlab.trading.portfolio_execution import PortfolioStore

    with (closing(DemoRuntime(root)) as runtime,
          open_session(root, credentials, runtime, dict(GET=0, POST=0, DELETE=0),
                       campaign=runtime.active_campaign()) as (session, _),
          closing(PortfolioStore(root / 'portfolio.sqlite3')) as capital):
        recovered = session.recover()
        assert recovered['reconciled'], recovered
        consumer = DemoPortfolioConsumer(session, runtime, root)
        capability = DemoInstanceCapability(session, runtime, dispatch_gate=lambda binding: None)
        owner = DemoInstanceOwner.open(session, capital, capability=capability)
        yield consumer, owner


def execute(root, exchange, observer):
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, bind_account
    from factorlab.integrations.binance_futures_trading.demo_session import demo_account_configuration, observe_demo_account
    from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment
    from factorlab.trading import Market
    from factorlab.trading.paper import Instrument

    credentials = DemoBinding()
    credential = Credentials('offline-mailbox-key', 'offline-mailbox-secret', Environment.DEMO)
    facts, _ = observe_demo_account(credential)
    binding = bind_account(credentials, credential, facts, demo_account_configuration(facts))
    with closing(DemoRuntime(root)) as runtime:
        epoch = runtime.stop_epoch()
        runtime.start_continuous(binding, opening_equity=D(1000), stop_epoch=epoch)
        runtime.ensure_window(binding, facts=facts, reconciled=True, stop_epoch=epoch)
    mailbox = root / 'tradfi-tasks.sqlite3'
    legacy = PortfolioTask('legacy-nanosecond', 'funded', '2026-11-30',
                           '2026-11-30T09:30:02-05:00', '2026-11-30T09:30:02.000000001-05:00',
                           D(10), {'BTCUSDT': D(0)})
    scheduled = 'tradfi-qqq-spy-test:2026-11-30:offline'
    acceptance = 'qqq-spy-acceptance'
    with closing(TaskMailbox(mailbox)) as box:
        assert box.submit(legacy) == 'QUEUED'
        # Normal execution of the scheduled target submits both QQQ and SPY.
        with open_execute(root, credentials) as (consumer, _):
            consumer.poll()
            assert status(box, scheduled)['status'] == 'RECONCILING', status(box, scheduled)
            original_ids = set(exchange.orders)
            assert {order['symbol'] for order in exchange.orders.values()} == set(CONFIG['weights']), (status(box, scheduled), exchange.orders)
            assert len(original_ids) == 2
        with open_execute(root, credentials) as (consumer, _):
            consumer.poll()
            assert set(exchange.orders) == original_ids and exchange.post_count == 2
            # The original batch is still in flight at the expiry boundary.
            exchange.now += timedelta(seconds=60)
            consumer.poll()
            assert status(box, scheduled)['detail'] == dict(reason='EXPIRED', recovered_inflight=True)
            assert all(exchange.orders[key]['status'] == 'CANCELED' for key in original_ids)
            consumer.poll()
            assert status(box, acceptance)['detail'] == dict(reason='EXPIRED', recovered_inflight=False)
            assert exchange.post_count == 2 and exchange.delete_count == 2

        # Publish another acceptance task with fresh observations through the Observer.
        accepted, dynamic = 'qqq-spy-live-window', 'dynamic-btc'
        subprocess.run([observer, '-m', __spec__.name, '--acceptance', str(root)], check=True)
        # The expired historical task is next. Make its pre-existing allocation available.
        with open_execute(root, credentials) as (consumer, owner):
            session = consumer.session
            instance = dict(account_id=binding['account_id'], market=Market.BINANCE_USDM.value,
                            instance_id='btc-sleeve', allocation_id='funded')
            owner.execution.executor.allocate(account_id=binding['account_id'], strategy_id='btc-sleeve',
                allocation_id='funded', capital=D(20), initial_cash=D(20), official_equity=D(1000))
            session.controller.store.record_metadata('demo:instance:btc-sleeve',
                dict(binding=instance, symbols=['BTCUSDT'], allocation=dict(budget_usdt=D(20)), shorting=False))
            session.controller._atomic(lambda: session.gateway.state.instruments.update(
                BTCUSDT=Instrument('BTCUSDT', 'USDT')))
            consumer.poll()
            assert status(box, legacy.task_id)['detail']['reason'] == 'EXPIRED'
            consumer.poll()
            assert status(box, accepted)['status'] == 'RECONCILING', status(box, accepted)
            assert exchange.post_count == 4
            exchange.now += timedelta(seconds=60)
            consumer.poll()
            assert status(box, accepted)['detail'] == dict(reason='EXPIRED', recovered_inflight=True)
            new = PortfolioTask(dynamic, 'funded', '2026-11-30', exchange.now.isoformat(),
                                (exchange.now + timedelta(seconds=1)).isoformat(), D(20), {'BTCUSDT': D('.5')})
            box.submit(new)
            consumer.poll()
            assert status(box, dynamic)['status'] == 'ACCEPTED', status(box, dynamic)
            owner.poll()
            assert exchange.post_count == 5, owner.execution.executor.store.db.execute('SELECT * FROM portfolio_plans').fetchall()
            dynamic_id = next(key for key, order in exchange.orders.items() if order['symbol'] == 'BTCUSDT')
        exchange.now += timedelta(seconds=1)
        with open_execute(root, credentials) as (consumer, owner):
            consumer.poll()
            assert status(box, dynamic)['detail']['reason'] == 'EXPIRED_INFLIGHT'
            owner.poll()
            consumer.poll()
            assert status(box, dynamic)['status'] == 'REJECTED', status(box, dynamic)
            assert exchange.orders[dynamic_id]['status'] == 'CANCELED'
            assert exchange.post_count == 5 and exchange.delete_count == 5

            filled = PortfolioTask('dynamic-fill', 'funded', '2026-11-30', exchange.now.isoformat(),
                                   (exchange.now + timedelta(seconds=60)).isoformat(),
                                   D(20), {'BTCUSDT': D('.3')})
            box.submit(filled)
            consumer.poll()
            owner.poll()
            filled_id = next(key for key, row in exchange.orders.items() if row['status'] == 'NEW')
            exchange.now += timedelta(seconds=1)
            exchange.fill_btc(filled_id)
            owner.poll()
            consumer.poll()
            assert status(box, filled.task_id)['status'] == 'COMPLETED', status(box, filled.task_id)
            assert exchange.post_count == 6 and exchange.delete_count == 5
    with closing(TaskMailbox(mailbox)) as box:
        saved = box.snapshot()
        assert box.submit(new) == 'REJECTED'
        assert box.submit(filled) == 'COMPLETED'
        assert box.submit(legacy) == 'REJECTED'
        box.receipt(dynamic, recorded_at=exchange.now + timedelta(seconds=1), status='REJECTED',
                    detail=status(box, dynamic)['detail'])
        assert box.snapshot() == saved and box.outstanding() == []
    # The account id is bound to this fixture's synthetic credentials on both architectures.
    return dict(receipts=saved, orders={key: {k: row[k] for k in ('symbol', 'origQty', 'price', 'status')}
                                       for key, row in exchange.orders.items()})


def fresh_acceptance(root):
    from factorlab.integrations.binance_futures_trading.tradfi_cloud import dispatch

    at = START + timedelta(seconds=60)
    quotes = {s: {'time': int(at.timestamp() * 1000)} for s in CONFIG['weights']}
    with closing(TaskMailbox(root / 'tradfi-tasks.sqlite3')) as box:
        assert dispatch(box, CONFIG, None, quotes, at, acceptance_task_id='qqq-spy-live-window')['status'] == 'QUEUED'


def restore_adaptive_grids():
    from factorlab.experiments.grid_funding_adaptive import demo_grid_rules, make_grid_config
    from factorlab.integrations.binance_futures_trading.demo_runtime import restore_grid
    from factorlab.strategies.grid_execution import GridAccount, GridExecution
    from factorlab.trading import Market

    grid = GridExecution(make_grid_config(150), demo_grid_rules(),
                         deployment_id='mailbox-grid', account_id='offline', instrument_id='NVDAUSDT',
                         market=Market.BINANCE_USDM, initial_equity=D(10))
    deployment = grid.deployment()
    funding = restore_grid(dict(deployment, r10_strategy='B_FUNDING_SENSITIVE_GRID',
                                funding_policy={}, funding_events=[]))
    volatility = restore_grid(dict(deployment, r10_strategy='C_VOL_ADAPTIVE_GRID',
                                   vol_policy={'vol_window_hours': 3}, warmup_closes=[149, 151, 150, 152]))
    account = GridAccount(inventory=D(0), available=D(10), equity=D(10), committed=D(0), turnover=D(0),
                          account_id='offline', instrument_id='NVDAUSDT', market=Market.BINANCE_USDM,
                          reconciled=True)
    observation = dict(id='first-observation', time='2026-11-30T14:30:02.000000001',
                       price=D(150), close=D(150), high=D(150), low=D(150), volume=0, completed_bar=True)
    for restored in (funding, volatility):
        restored.observe(observation, account, delivery=1)
        assert restored.delivery == 1
    return dict(funding_delivery=funding.delivery, volatility_delivery=volatility.delivery,
                volatility=round(volatility.current_vol_1h, 12))


def main():
    if sys.argv[1] == '--observe':
        produce(Path(sys.argv[2]))
        return
    if sys.argv[1] == '--acceptance':
        fresh_acceptance(Path(sys.argv[2]))
        return
    with TemporaryDirectory() as directory:
        subprocess.run([sys.argv[1], '-m', __spec__.name, '--observe', directory], check=True)
        attempted = []

        def record_import(event, args):
            if event == 'import' and args[0].split('.')[0] in {'pandas', 'numpy'}:
                attempted.append(args[0])

        sys.addaudithook(record_import)
        exchange = MailboxExchange()
        with (local_exchange(exchange), patch.dict(os.environ,
              {'FACTORLAB_DEMO_TASK_DB': str(Path(directory) / 'tradfi-tasks.sqlite3')})):
            results = execute(Path(directory), exchange, sys.argv[1])
        results['adaptive_grids'] = restore_adaptive_grids()
    assert not attempted, attempted
    assert not any(name.split('.')[0] in {'pandas', 'numpy'} for name in sys.modules)
    print(f'{platform.system()} {platform.machine()} Python {platform.python_version()}', file=sys.stderr)
    print(json.dumps(results, sort_keys=True))


if __name__ == '__main__':
    main()
