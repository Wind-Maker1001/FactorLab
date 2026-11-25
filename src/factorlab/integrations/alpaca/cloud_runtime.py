"""Paper account observation lifecycle; execution ownership transfers separately."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import threading
from zoneinfo import ZoneInfo

from .adapter import AlpacaAdapter
from .http import AlpacaConfig, AuthenticationError, PaperHTTP, PermissionDenied, RateLimited, ServiceError
from .market_data.client import Credentials, MarketDataClient, MarketDataError
from .market_data.service import MarketDataService
from .market_data.stream import AlpacaStream, StreamDisconnected, StreamRecoveryError
from .stream import trade_updates

NY = ZoneInfo('America/New_York')
SYMBOLS = ('AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ')


def now():
    return datetime.now(timezone.utc)


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as output:
        json.dump(value, output, default=str, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


@contextmanager
def owner_lock(root):
    import fcntl
    with (root / 'owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def private_root(path):
    root = Path(path)
    info = root.stat()
    if not root.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('state root must be an existing absolute private directory')
    if info.st_uid != os.getuid():
        raise ValueError('state root must belong to the service identity')
    return root


def credential_config(path):
    from factorlab.integrations.binance_futures_trading.demo_file_credentials import DemoFileCredentialStore
    path = Path(path)
    credential_directory = os.environ.get('CREDENTIALS_DIRECTORY')
    systemd = credential_directory is not None and path.parent == Path(credential_directory)
    directory = DemoFileCredentialStore._open_directory(path.parent)
    try:
        if systemd and not DemoFileCredentialStore._systemd_credential_acl_ok(directory, directory=True):
            raise ValueError('unsafe systemd credential directory')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, encoding='utf-8-sig') as stream:
            if systemd:
                if not DemoFileCredentialStore._systemd_credential_acl_ok(stream.fileno()):
                    raise ValueError('unsafe systemd credential ACL')
            else:
                DemoFileCredentialStore._private(stream.fileno())
            values = json.load(stream)
            return AlpacaConfig(values['APCA_API_KEY_ID'], values['APCA_API_SECRET_KEY'],
                                values.get('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets'))
    finally:
        os.close(directory)


def weekly_window(calendar, at):
    local = at.astimezone(NY)
    day = local.date().isoformat()
    session = calendar.regular_session(at)
    first = calendar.first_session(day)
    previous = calendar.schedule.index[calendar.schedule.index < first][-1]
    return {'week': (local.date() - timedelta(days=local.weekday())).isoformat(),
            'execution_session': first.date().isoformat(),
            'signal_cutoff': previous.date().isoformat(),
            'due': session is not None and session == first,
            'market_open': session is not None,
            'next_open': calendar.next_session(at).open.isoformat()}


class ObservationHTTP(PaperHTTP):
    def request(self, method, path, **kwargs):
        if method != 'GET':
            raise PermissionError('observation runtime cannot submit or cancel orders')
        return super().request(method, path, **kwargs)


class RuntimeStore:
    def __init__(self, root):
        self.root = root
        self.db = sqlite3.connect(root / 'runtime.sqlite3')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError('runtime schema incompatible with this release')
        self.db.execute('PRAGMA user_version=1')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_reports (
                day TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery (
                id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL);
        ''')
        self.starts = int(self.get('starts', 0)) + 1
        self.put('starts', self.starts)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value, default=str)))

    def event(self, kind, payload):
        with self.db:
            self.db.execute('INSERT INTO recovery(observed_at,kind,payload) VALUES (?,?,?)',
                            (now().isoformat(), kind, json.dumps(payload, default=str)))

    def report(self, at, payload):
        day = at.astimezone(NY).date().isoformat()
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO daily_reports VALUES (?,?,?)',
                            (day, at.isoformat(), json.dumps(payload, default=str)))
        directory = self.root / 'reports'
        directory.mkdir(mode=0o700, exist_ok=True)
        atomic_json(directory / (day + '.json'), payload)


class MarketWorker:
    """Separate connection and SQLite owner; no account or order API access."""
    def __init__(self, config, root, symbols, stop, *, feed='iex'):
        self.config, self.root, self.symbols, self.stop = config, root, tuple(symbols), stop
        self.feed = feed
        self.loop, self.changed = None, None
        self.lock = threading.Lock()
        self.state = {'state': 'STARTING', 'last_event_at': None, 'last_rest_at': None,
                      'recoveries': 0, 'error': None}
        self.thread = threading.Thread(target=self.run, name='alpaca-market', daemon=True)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def replace_symbols(self, symbols):
        symbols = tuple(sorted(set(symbols)))
        with self.lock:
            if symbols == tuple(sorted(self.symbols)):
                return
            self.symbols = symbols
            if self.loop is not None:
                self.loop.call_soon_threadsafe(self.changed.set)

    def run(self):
        try:
            asyncio.run(self.consume())
        except Exception as error:
            self.update(state='FAILED', error=type(error).__name__)

    async def consume(self):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        with self.lock:
            self.loop, self.changed = asyncio.get_running_loop(), asyncio.Event()
        client = MarketDataClient(Credentials(self.config.key_id, self.config.secret_key),
                                  feed=self.feed, attempts=1)
        db = sqlite3.connect(self.root / 'market-stream.sqlite3')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, channel TEXT NOT NULL,
                symbol TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gaps (
                start TEXT NOT NULL, end TEXT NOT NULL, rows INTEGER NOT NULL);
        ''')
        row = db.execute('SELECT end FROM gaps ORDER BY rowid DESC LIMIT 1').fetchone()
        last = datetime.fromisoformat(row[0]) if row else now() - timedelta(minutes=5)

        async def recover_gap(*, start, end, **_):
            self.update(state='RECOVERING')
            # Latest quotes cannot repair missing bars. Store the real REST gap rows.
            rows = await asyncio.to_thread(client.bars, self.symbols, start.isoformat(), end.isoformat(),
                                           timeframe='1Min', asof=end.astimezone(NY).date().isoformat())
            with db:
                db.executemany('INSERT INTO events(received_at,channel,symbol,payload) VALUES (?,?,?,?)',
                               [(r['received_at'], 'rest-bars', r['symbol'], json.dumps(r)) for r in rows])
                db.execute('INSERT INTO gaps VALUES (?,?,?)', (start.isoformat(), end.isoformat(), len(rows)))
            self.update(state='CONNECTED', last_rest_at=end.isoformat(),
                        recoveries=self.snapshot()['recoveries'] + 1, error=None)
            return True

        async def events():
            nonlocal last
            delay = 1
            while not self.stop.is_set():
                try:
                    await recover_gap(start=last, end=now())
                    stream = AlpacaStream(api_key=self.config.key_id, secret_key=self.config.secret_key,
                        feed=self.feed, subscriptions={'bars': self.symbols, 'quotes': self.symbols},
                        recover_gap=recover_gap, max_reconnects=3)
                    async for event in stream.events():
                        last = event.received_at
                        with db:
                            db.execute('INSERT INTO events(received_at,channel,symbol,payload) VALUES (?,?,?,?)',
                                       (event.received_at.isoformat(), event.channel, event.symbol,
                                        json.dumps(event.payload)))
                        self.update(state='CONNECTED', last_event_at=event.received_at.isoformat(), error=None)
                        delay = 1
                except (StreamDisconnected, StreamRecoveryError, MarketDataError,
                        ConnectionClosed, InvalidHandshake, OSError, TimeoutError) as error:
                    self.update(state='RECONNECTING', error=type(error).__name__)
                    await asyncio.sleep(delay)
                    delay = min(60, delay * 2)

        async def wait_for_stop():
            while not self.stop.is_set():
                await asyncio.sleep(1)

        task = asyncio.create_task(events())
        stopped = asyncio.create_task(wait_for_stop())
        changed = asyncio.create_task(self.changed.wait())
        try:
            while True:
                done, _ = await asyncio.wait((task, stopped, changed), return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    break
                if task in done:
                    await task
                    break
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.changed.clear()
                task = asyncio.create_task(events())
                changed = asyncio.create_task(self.changed.wait())
        finally:
            with self.lock:
                self.loop = None
            task.cancel()
            stopped.cancel()
            changed.cancel()
            await asyncio.gather(task, stopped, changed, return_exceptions=True)
            db.close()


class Supervisor:
    def __init__(self, root, config, *, symbols=SYMBOLS, interval=15, revision='unknown',
                 execute_v69=False, instances_configuration=None, feed='iex'):
        self.root, self.config, self.symbols = root, config, tuple(symbols)
        self.interval, self.revision = interval, revision
        self.stop = threading.Event()
        self.store = RuntimeStore(root)
        if execute_v69 and feed != 'iex':
            raise ValueError('original V69 input provenance requires IEX')
        if execute_v69 and instances_configuration is not None:
            raise ValueError('legacy V69 and generic instance configuration cannot own the same runtime')
        enabled = execute_v69 or instances_configuration is not None
        self.adapter = AlpacaAdapter((PaperHTTP if enabled else ObservationHTTP)(config), root / 'account.sqlite3')
        self.execution = None
        if execute_v69:
            from .native_v69 import NativeWeeklyExecution
            self.execution = NativeWeeklyExecution(root, self.adapter, clock=now, revision=revision)
        if instances_configuration is not None:
            from .strategy_instances import StrategyInstances
            self.execution = StrategyInstances(root, self.adapter, instances_configuration,
                                               clock=now, revision=revision)
            self.symbols = self.execution.symbols
            if not self.symbols:
                raise ValueError('generic runtime requires a registered active or pending package')
        self.client = MarketDataClient(Credentials(config.key_id, config.secret_key), feed=feed, attempts=1)
        self.market = MarketWorker(config, root, self.symbols, self.stop, feed=feed)
        self.started = now()
        self.last_poll = None
        self.calendar_day = None
        self.service = None
        self.health = {'account_recovered': False, 'last_recovery_at': None,
                       'trade_stream': 'STARTING', 'error': None}

    def publish(self):
        at = now()
        value = dict(self.health, heartbeat=at.isoformat(), started_at=self.started.isoformat(),
                     pid=os.getpid(), starts=self.store.starts, revision=self.revision,
                     mode='EXECUTE' if self.execution else 'OBSERVE', market=self.market.snapshot(),
                     strategy=self.execution.state if self.execution else
                         {'state': 'NOT_ENABLED', 'name': 'v69-original', 'frequency': 'natural-week'},
                     execution=self.execution.execution if self.execution else
                         {'state': 'WAITING_E03_HANDOVER', 'enabled': False})
        rest = value.get('market_rest')
        market_open = value.get('schedule', {}).get('market_open', False)
        value['market_available'] = bool(rest and not rest['missing_symbols']
            and (not market_open or rest['fresh_quotes']))
        value['healthy'] = (value['account_recovered'] and value['error'] is None
                            and value['trade_stream'] == 'CONNECTED'
                            and value['market_available'] and value['market']['state'] == 'CONNECTED')
        atomic_json(self.root / 'health.json', value)
        return value

    def poll(self):
        at = now()
        if self.last_poll is not None and (at - self.last_poll).total_seconds() < self.interval:
            return
        self.last_poll = at
        self.health.update(account_recovered=False, market_rest=None, error=None)
        try:
            snapshot = self.adapter.recover()
            facts = self.adapter.read_account_details()
            self.health.update(account_recovered=snapshot.reconciled,
                               last_recovery_at=at.isoformat())
            if self.calendar_day != at.date():
                start, end = at.date() - timedelta(days=500), at.date() + timedelta(days=370)
                self.service = MarketDataService.connect(self.client, None, start=start, end=end)
                self.calendar_day = at.date()
            window = weekly_window(self.service.calendar, at)
            quotes, received = self.client.latest('quotes', self.symbols)
            clock, _ = self.client.clock()
            # Requests are already retained in bounded operational summaries below.
            self.client.evidence.clear()
            quote_rows = quotes.get('quotes', {})
            missing = sorted(symbol for symbol in self.symbols if not quote_rows.get(symbol))
            fresh = not missing and all(0 <= (received - datetime.fromisoformat(
                quote_rows[symbol]['t'].replace('Z', '+00:00'))).total_seconds() <= 90
                for symbol in self.symbols)
            self.health.update(account_recovered=snapshot.reconciled,
                               last_recovery_at=at.isoformat(), schedule=window,
                               market_rest={'received_at': received.isoformat(), 'missing_symbols': missing,
                                            'feed': self.client.feed, 'clock_open': clock['is_open'],
                                            'fresh_quotes': fresh})
            self.store.event('RECOVERED', {'reconciled': snapshot.reconciled})
            if self.execution and snapshot.reconciled:
                from factorlab.trading.control import RiskLimits
                self.adapter.limits = RiskLimits(facts.equity, facts.equity, facts.equity, 30)
                self.execution.tick(self.service, window, at, facts)
                if 'instances' in self.execution.state:
                    self.symbols = self.execution.symbols
                    self.market.replace_symbols(self.symbols)
                snapshot = self.adapter.recover()
                facts = self.adapter.read_account_details()
                self.health['account_recovered'] = snapshot.reconciled
            report = {'observed_at': at.isoformat(), 'source': 'ALPACA_PAPER_ACCOUNT',
                      'equity': str(facts.equity), 'cash': str(facts.cash),
                      'positions': self.adapter.http.request('GET', '/v2/positions'),
                      'target_weights': None, 'actual_weights': {},
                      'strategy_state': self.execution.state['state'] if self.execution else 'NOT_ENABLED',
                      'execution_state': self.execution.execution['state'] if self.execution else 'WAITING_E03_HANDOVER',
                      'fills': [row for row in self.adapter.store.activities() if
                          datetime.fromisoformat(row['transaction_time'].replace('Z', '+00:00')).astimezone(NY).date()
                          == at.astimezone(NY).date()],
                      'schedule': window}
            baseline = self.store.get('equity_baseline')
            if baseline is None:
                baseline = {'equity': str(facts.equity), 'at': at.isoformat()}
                self.store.put('equity_baseline', baseline)
            report['equity_baseline'] = baseline
            # Observation equity change is not strategy return (external cash flows may exist).
            from decimal import Decimal
            report['equity_change_since_baseline'] = str(facts.equity - Decimal(baseline['equity']))
            report['strategy_return'] = None
            if self.execution and 'instances' in self.execution.state:
                report['instances'] = self.execution.report()
            if self.execution and self.execution.identity:
                from dataclasses import asdict
                plan = self.execution.store.plan(self.execution.identity)
                report['target_weights'] = {s: str(w) for s, w in plan['request']['weights'].items()}
                report['original_v69_task'] = dict(identity=self.execution.identity,
                    signal_inputs=plan['request']['signal_inputs'], accounting=None)
                report['execution_ledger_return'] = None
                if self.execution.execution['state'] != 'BLOCKED_INPUTS':
                    accounting = self.execution.runner.report(self.execution.identity)
                    report['original_v69_task']['accounting'] = asdict(accounting)
                    report['execution_ledger_return'] = str(
                        (accounting.realized_pnl + accounting.unrealized_pnl + accounting.corporate_cash_flow)
                        / accounting.initial_capital)
            report['actual_weights'] = {p['symbol']: str(Decimal(p['market_value']) / facts.equity)
                                        for p in report['positions']} if facts.equity else {}
            self.store.report(at, report)
        except (ConnectionError, TimeoutError, OSError, RateLimited, ServiceError, MarketDataError) as error:
            self.health.update(error=type(error).__name__)
            self.store.event('RECOVERY_FAILED', {'error': type(error).__name__,
                                               'account_recovered': self.health['account_recovered']})
            if isinstance(error, MarketDataError) and error.status in {401, 403}:
                raise PermissionError('market authentication refused') from None
        self.publish()

    @contextmanager
    def connection(self, url, **kwargs):
        from websockets.sync.client import connect
        supervisor = self
        with connect(url, **kwargs) as socket:
            class Socket:
                handshakes = 0

                def send(self, message):
                    socket.send(message)

                def recv(self, timeout):
                    if self.handshakes < 2:
                        self.handshakes += 1
                        return socket.recv(timeout=timeout)
                    while not supervisor.stop.is_set():
                        supervisor.poll()
                        try:
                            return socket.recv(timeout=min(timeout, supervisor.interval))
                        except TimeoutError:
                            continue
                    raise InterruptedError('service stopping')
            yield Socket()

    def on_update(self):
        self.health['trade_stream'] = 'CONNECTED'
        self.poll()
        self.publish()
        return self.stop.is_set()

    def run(self):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        self.store.event('START', {'revision': self.revision, 'pid': os.getpid(), 'starts': self.store.starts})
        self.market.thread.start()
        delay = 1
        try:
            while not self.stop.is_set():
                try:
                    self.poll()
                    trade_updates(self.adapter, connect=self.connection, on_update=self.on_update)
                    delay = 1
                except (ConnectionError, TimeoutError, OSError, RateLimited, ServiceError,
                        ConnectionClosed, InvalidHandshake) as error:
                    self.health.update(trade_stream='RECONNECTING', account_recovered=False,
                                       error=type(error).__name__)
                    self.store.event('STREAM_DISCONNECTED', {'error': type(error).__name__})
                    self.publish()
                    self.stop.wait(delay)
                    delay = min(60, delay * 2)
        finally:
            self.stop.set()
            self.market.thread.join(timeout=65)
            self.health.update(trade_stream='STOPPED', account_recovered=False)
            self.publish()
            self.store.event('STOP', {'pid': os.getpid()})
            if self.execution:
                self.execution.close()
            self.adapter.store.db.close()
            self.store.db.close()


def health(root, *, at=None, max_age=90):
    value = json.loads((Path(root) / 'health.json').read_text(encoding='utf-8'))
    age = ((at or now()) - datetime.fromisoformat(value['heartbeat'])).total_seconds()
    value['heartbeat_age_seconds'] = age
    value['healthy'] = value['healthy'] and 0 <= age <= max_age and value['trade_stream'] != 'STOPPED'
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'health', 'acceptance'))
    parser.add_argument('--phase', choices=('open', 'recover', 'rotate', 'replay', 'cleanup'))
    parser.add_argument('--state-root', default='/var/lib/factorlab-alpaca-paper')
    parser.add_argument('--credential-file', default=os.environ.get('FACTORLAB_ALPACA_SECRET_FILE'))
    parser.add_argument('--revision', default='unknown')
    parser.add_argument('--execute-v69', action='store_true',
                        default=os.environ.get('FACTORLAB_ALPACA_EXECUTE_V69') == '1')
    parser.add_argument('--symbols', default=','.join(SYMBOLS))
    parser.add_argument('--instances', type=Path)
    parser.add_argument('--feed', choices=('iex', 'sip'), default='iex')
    args = parser.parse_args(argv)
    if args.action == 'acceptance' and args.phase is None:
        parser.error('acceptance requires --phase')
    if args.action != 'acceptance' and args.phase is not None:
        parser.error('--phase is only valid for acceptance')
    if args.action == 'health':
        try:
            value = health(args.state_root)
        except FileNotFoundError:
            value = {'healthy': False, 'state': 'NOT_STARTED'}
        print(json.dumps(value, default=str))
        return 0 if value['healthy'] else 1
    os.umask(0o077)
    try:
        root = private_root(args.state_root)
        config = credential_config(args.credential_file)
        with owner_lock(root):
            if args.action == 'acceptance':
                from .portfolio_acceptance import run_acceptance
                result = run_acceptance(state_dir=root, phase=args.phase, runtime_config=config,
                                        campaign='ALP-E04 CLOUD TEST')
                print(json.dumps(result), flush=True)
                return 0
            instances = (json.loads(args.instances.read_text(encoding='utf-8'))
                         if args.instances is not None else None)
            runtime = Supervisor(root, config, symbols=args.symbols.split(','), revision=args.revision,
                                 execute_v69=args.execute_v69, instances_configuration=instances,
                                 feed=args.feed)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: runtime.stop.set())
            runtime.run()
    except (ValueError, PermissionError, AuthenticationError, PermissionDenied, sqlite3.DatabaseError) as error:
        print(json.dumps({'state': 'REFUSED', 'error': type(error).__name__}), flush=True)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
