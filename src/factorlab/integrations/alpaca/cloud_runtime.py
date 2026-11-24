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
SYMBOLS = ('QQQ', 'AAPL', 'SPY', 'TLT', 'GLD', 'IEF', 'SHY', 'BIL')


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
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError('private regular credential file required')
    # systemd mounts root-owned credentials read-only for the service UID.
    if info.st_uid not in {0, os.getuid()}:
        raise ValueError('credential owner must be root or the service identity')
    return AlpacaConfig.from_file(path)


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


# TODO(ALP-E03): connect persisted portfolio tasks after the committed runner is
# available and both account journals have transferred from the previous owner.
# TODO(V69-F02): use the original strategy entry point; never substitute weights.
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
    def __init__(self, config, root, symbols, stop):
        self.config, self.root, self.symbols, self.stop = config, root, symbols, stop
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

    def run(self):
        try:
            asyncio.run(self.consume())
        except Exception as error:
            self.update(state='FAILED', error=type(error).__name__)

    async def consume(self):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        client = MarketDataClient(Credentials(self.config.key_id, self.config.secret_key),
                                  feed='iex', attempts=1)
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
                        feed='iex', subscriptions={'bars': self.symbols, 'quotes': self.symbols},
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

        task = asyncio.create_task(events())
        stopped = asyncio.create_task(asyncio.to_thread(self.stop.wait))
        try:
            done, _ = await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            db.close()


class Supervisor:
    def __init__(self, root, config, *, symbols=SYMBOLS, interval=15, revision='unknown'):
        self.root, self.config, self.symbols = root, config, tuple(symbols)
        self.interval, self.revision = interval, revision
        self.stop = threading.Event()
        self.store = RuntimeStore(root)
        self.adapter = AlpacaAdapter(ObservationHTTP(config), root / 'account.sqlite3')
        self.client = MarketDataClient(Credentials(config.key_id, config.secret_key), feed='iex', attempts=1)
        self.market = MarketWorker(config, root, self.symbols, self.stop)
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
                     mode='OBSERVE', market=self.market.snapshot(),
                     strategy={'state': 'WAITING_F02', 'name': 'v69-original', 'frequency': 'natural-week'},
                     execution={'state': 'WAITING_E03_HANDOVER', 'enabled': False})
        value['healthy'] = (value['account_recovered'] and value['error'] is None
                            and value['market']['state'] != 'FAILED')
        atomic_json(self.root / 'health.json', value)
        return value

    def poll(self):
        at = now()
        if self.last_poll is not None and (at - self.last_poll).total_seconds() < self.interval:
            return
        self.last_poll = at
        self.health.update(account_recovered=False, error=None)
        try:
            snapshot = self.adapter.recover()
            facts = self.adapter.read_account_details()
            if self.calendar_day != at.date():
                start, end = at.date() - timedelta(days=40), at.date() + timedelta(days=370)
                self.service = MarketDataService.connect(self.client, None, start=start, end=end)
                self.calendar_day = at.date()
            window = weekly_window(self.service.calendar, at)
            quotes, received = self.client.latest('quotes', self.symbols)
            clock, _ = self.client.clock()
            # Requests are already retained in bounded operational summaries below.
            self.client.evidence.clear()
            missing = sorted(set(self.symbols) - set(quotes.get('quotes', {})))
            self.health.update(account_recovered=snapshot.reconciled,
                               last_recovery_at=at.isoformat(), schedule=window,
                               market_rest={'received_at': received.isoformat(), 'missing_symbols': missing,
                                            'feed': 'iex', 'clock_open': clock['is_open']})
            self.store.event('RECOVERED', {'reconciled': snapshot.reconciled})
            report = {'observed_at': at.isoformat(), 'source': 'ALPACA_PAPER_OBSERVATION',
                      'equity': str(facts.equity), 'cash': str(facts.cash),
                      'positions': self.adapter.http.request('GET', '/v2/positions'),
                      'target_weights': None, 'actual_weights': {},
                      'strategy_state': 'WAITING_F02', 'execution_state': 'WAITING_E03_HANDOVER',
                      'fills': self.adapter.http.request('GET', '/v2/account/activities/FILL',
                          params={'date': at.astimezone(NY).date().isoformat()}),
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
            report['actual_weights'] = {p['symbol']: str(Decimal(p['market_value']) / facts.equity)
                                        for p in report['positions']} if facts.equity else {}
            self.store.report(at, report)
        except (ConnectionError, TimeoutError, OSError, RateLimited, ServiceError, MarketDataError) as error:
            self.health.update(account_recovered=False, error=type(error).__name__)
            self.store.event('RECOVERY_FAILED', {'error': type(error).__name__})
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
    parser.add_argument('action', choices=('run', 'health'))
    parser.add_argument('--state-root', default='/var/lib/factorlab-alpaca-paper')
    parser.add_argument('--credential-file', default=os.environ.get('FACTORLAB_ALPACA_SECRET_FILE'))
    parser.add_argument('--revision', default='unknown')
    parser.add_argument('--symbols', default=','.join(SYMBOLS))
    args = parser.parse_args(argv)
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
            runtime = Supervisor(root, config, symbols=args.symbols.split(','), revision=args.revision)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: runtime.stop.set())
            runtime.run()
    except (ValueError, PermissionError, AuthenticationError, PermissionDenied, sqlite3.DatabaseError) as error:
        print(json.dumps({'state': 'REFUSED', 'error': type(error).__name__}), flush=True)
        return 78
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
