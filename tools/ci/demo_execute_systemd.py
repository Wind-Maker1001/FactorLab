"""Real Linux systemd acceptance for P8-C04 Binance Demo Grid Execute (offline loopback stub)."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import sqlite3
import subprocess
import tempfile
from threading import Lock, Thread
import time
from urllib.parse import parse_qs, urlsplit


ROOT = '/var/lib/factorlab-demo-c04'
HALTED_ROOT = '/var/lib/factorlab-demo-c04-halted'
CORRUPT_ROOT = '/var/lib/factorlab-demo-c04-corrupt'
SECRET_PATH = '/etc/factorlab-demo/c04-acceptance-secret.json'
HARNESS_DIR = '/opt/factorlab/c04-harness'
VENV = '/opt/factorlab/venv/bin'
PYTHON = f'{VENV}/python'
DEMO_CLI = f'{VENV}/factorlab-demo'
CLOUD_MOD = 'factorlab.integrations.binance_futures_trading.demo_cloud_runtime'

EXEC_UNIT = 'factorlab-demo-execute-c04.service'
GET_UNIT = 'factorlab-demo-getonly-c04.service'
HALT_UNIT = 'factorlab-demo-execute-c04-halted.service'
CORRUPT_UNIT = 'factorlab-demo-execute-c04-corrupt.service'

KEY_SENTINEL = 'C04_TEST_API_KEY_SENTINEL_91827364'
SECRET_SENTINEL = 'C04_TEST_API_SECRET_SENTINEL_46372819'


class OfflineDemoExchange:
    """Thread-safe loopback Binance USD-M Demo HTTP state for offline systemd acceptance."""

    def __init__(self):
        self.lock = Lock()
        now_ms = int(time.time() * 1000)
        # Keep server_ms 1..60s behind wall clock so health freshness stays non-negative,
        # while placing the first 1m bar boundary ~4 seconds ahead.
        rem = now_ms % 60_000
        self.offset_ms = ((56_000 - rem) % 60_000) - 60_000
        self.outage = False
        self.auth_reject = False
        self.timeout_next_post = False
        self.breakout_bar_open = None
        self.orders = {}
        self.trades = []
        self.cash = Decimal('1000')
        self.quantity = Decimal('0')
        self.entry = Decimal('0')
        self.post_count = 0
        self.delete_count = 0
        self.get_count = 0

    def align_next_bar(self, seconds_ahead=6):
        with self.lock:
            now_ms = int(time.time() * 1000)
            target_rem = (60_000 - seconds_ahead * 1000) % 60_000
            rem = now_ms % 60_000
            self.offset_ms = ((target_rem - rem) % 60_000) - 60_000

    def advance_toward_next_bar(self):
        """Advance clock strictly forward while staying behind host wall clock (no regression)."""
        with self.lock:
            now_ms = int(time.time() * 1000)
            current_rem = (now_ms + self.offset_ms) % 60_000
            needed = (54_000 - current_rem) % 60_000
            max_safe = max(0, -2_000 - self.offset_ms)
            self.offset_ms += min(needed, max_safe)

    def server_ms(self):
        return int(time.time() * 1000) + self.offset_ms

    def fill_order(self, client_order_id):
        with self.lock:
            row = self.orders[client_order_id]
            qty = Decimal(row['origQty']) - Decimal(row['executedQty'])
            price = Decimal(row['price'])
            if qty <= 0:
                return
            quote = Decimal(row['cumQuote']) + qty * price
            fee = qty * price * Decimal('0.0004')
            signed = qty if row['side'] == 'BUY' else -qty
            if self.quantity * signed < 0:
                self.cash += min(abs(self.quantity), qty) * (price - self.entry) * (1 if self.quantity > 0 else -1)
            new_qty = self.quantity + signed
            if self.quantity * signed >= 0:
                self.entry = (abs(self.quantity) * self.entry + qty * price) / abs(new_qty)
            elif self.quantity * new_qty <= 0:
                self.entry = price if new_qty else Decimal('0')
            self.quantity = new_qty
            self.cash -= fee
            row.update(executedQty=str(Decimal(row['origQty'])), cumQuote=str(quote),
                       status='FILLED', updateTime=self.server_ms())
            self.trades.append(dict(
                symbol='NVDAUSDT', id=len(self.trades) + 1, orderId=row['orderId'],
                qty=str(qty), quoteQty=str(qty * price), commission=str(fee), commissionAsset='USDT',
            ))

    def handle(self, method, path, query):
        with self.lock:
            if self.outage:
                return None, None
            if method == 'GET':
                self.get_count += 1
            elif method == 'POST':
                self.post_count += 1
            elif method == 'DELETE':
                self.delete_count += 1
            if self.auth_reject and path in {'/fapi/v3/account', '/fapi/v3/positionRisk', '/fapi/v1/openOrders'}:
                return 401, {'code': -2015, 'msg': 'Invalid API-key, IP, or permissions for action'}
            ms = self.server_ms()
            p = {k: v[0] for k, v in parse_qs(query).items()}
            if path == '/fapi/v1/time':
                return 200, {'serverTime': ms}
            if path == '/fapi/v1/exchangeInfo':
                return 200, {'symbols': [{
                    'symbol': 'NVDAUSDT', 'status': 'TRADING', 'quoteAsset': 'USDT',
                    'marginAsset': 'USDT', 'orderTypes': ['MARKET', 'LIMIT'],
                    'timeInForce': ['GTC'],
                    'filters': [
                        {'filterType': 'LOT_SIZE', 'minQty': '0.01', 'maxQty': '100', 'stepSize': '0.01'},
                        {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.01', 'maxQty': '100', 'stepSize': '0.01'},
                        {'filterType': 'PRICE_FILTER', 'minPrice': '0.01', 'maxPrice': '10000', 'tickSize': '0.01'},
                        {'filterType': 'MIN_NOTIONAL', 'notional': '5'},
                        {'filterType': 'PERCENT_PRICE', 'multiplierDown': '0.97', 'multiplierUp': '1.03'},
                        {'filterType': 'MAX_NUM_ORDERS', 'limit': 200},
                        {'filterType': 'POSITION_RISK_CONTROL', 'positionControlSide': 'NONE'},
                    ],
                }]}
            if path == '/fapi/v1/leverageBracket':
                return 200, [{'symbol': 'NVDAUSDT', 'brackets': [{
                    'bracket': 1, 'initialLeverage': 5, 'notionalFloor': '0',
                    'notionalCap': '10000', 'maintMarginRatio': '0.01', 'cum': '0',
                }]}]
            if path == '/fapi/v1/positionSide/dual':
                return 200, {'dualSidePosition': False}
            if path == '/fapi/v1/symbolConfig':
                return 200, [{'symbol': 'NVDAUSDT', 'leverage': 1, 'marginType': 'CROSSED'}]
            if path == '/fapi/v1/apiTradingStatus':
                return 200, {'indicators': {'NVDAUSDT': [{'indicator': 'UFR', 'isLocked': False}]}}
            if path == '/fapi/v1/premiumIndex':
                return 200, {'symbol': 'NVDAUSDT', 'markPrice': '150', 'time': ms}
            if path == '/fapi/v1/ticker/bookTicker':
                return 200, {'symbol': 'NVDAUSDT', 'bidPrice': '150', 'askPrice': '150', 'time': ms - 100}
            if path == '/fapi/v1/klines':
                start = int(p['startTime'])
                end = int(p['endTime'])
                rows = [[at, '150', '155', '147', '155', '10', at + 59_999]
                        if at == self.breakout_bar_open else
                        [at, '150', '153', '147', '150', '10', at + 59_999]
                        for at in range(start, end + 1, 60_000)]
                return 200, rows
            if path == '/fapi/v3/account':
                collateral = abs(self.quantity) * self.entry
                positions = [] if not self.quantity else [{
                    'symbol': 'NVDAUSDT', 'positionSide': 'BOTH',
                    'positionAmt': str(self.quantity), 'unrealizedProfit': '0',
                    'initialMargin': str(collateral), 'maintMargin': str(collateral * Decimal('0.01')),
                }]
                return 200, {
                    'canTrade': True,
                    'assets': [{
                        'asset': 'USDT', 'walletBalance': str(self.cash),
                        'availableBalance': str(self.cash - collateral),
                        'marginBalance': str(self.cash),
                        'initialMargin': str(collateral),
                        'maintMargin': str(collateral * Decimal('0.01')),
                        'unrealizedProfit': '0',
                    }],
                    'positions': positions,
                }
            if path == '/fapi/v3/positionRisk':
                if not self.quantity:
                    return 200, []
                return 200, [{
                    'symbol': 'NVDAUSDT', 'positionSide': 'BOTH',
                    'positionAmt': str(self.quantity), 'entryPrice': str(self.entry),
                    'markPrice': '150', 'notional': str(self.quantity * Decimal('150')),
                    'liquidationPrice': '0', 'isolatedMargin': '0',
                }]
            if path == '/fapi/v1/openOrders':
                return 200, [dict(o) for o in self.orders.values() if o['status'] in {'NEW', 'PARTIALLY_FILLED'}]
            if path == '/fapi/v1/allOrders':
                return 200, [dict(o) for o in self.orders.values()]
            if path == '/fapi/v1/userTrades':
                return 200, [t for t in self.trades if 'orderId' not in p or str(t['orderId']) == p['orderId']]
            if path == '/fapi/v1/income':
                return 200, []
            if path == '/fapi/v1/order':
                key = p.get('newClientOrderId', p.get('origClientOrderId'))
                if method == 'POST':
                    if key in self.orders:
                        return 400, {'code': -4116}
                    self.orders[key] = dict(
                        clientOrderId=key, orderId=len(self.orders) + 1,
                        symbol=p['symbol'], side=p['side'], type=p['type'],
                        positionSide=p['positionSide'], timeInForce=p.get('timeInForce', 'GTC'),
                        reduceOnly=p.get('reduceOnly', 'false'),
                        price=p['price'], origQty=p['quantity'],
                        executedQty='0', cumQuote='0', status='NEW', updateTime=ms,
                    )
                    if self.timeout_next_post:
                        self.timeout_next_post = False
                        return 'DROP', None
                    return 200, dict(self.orders[key])
                if key not in self.orders:
                    return 400, {'code': -2013}
                row = self.orders[key]
                if method == 'DELETE' and row['status'] != 'FILLED':
                    row['status'] = 'CANCELED'
                    row['updateTime'] = ms
                return 200, dict(row)
            return 404, {'code': -1000, 'msg': f'unknown path {path}'}


def start_stub_server(exchange):
    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self):
            parsed = urlsplit(self.path)
            status, payload = exchange.handle(self.command, parsed.path, parsed.query)
            if status is None:
                self.close_connection = True
                return
            if status == 'DROP':
                # Close socket without HTTP response to simulate write OutcomeUnknown.
                self.close_connection = True
                return
            body = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_DELETE = _dispatch

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def run_cmd(cmd, *, check=True, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def systemctl(*args, check=True):
    return run_cmd(['sudo', 'systemctl', *args], check=check).stdout.strip()


def as_service(args, *, root=ROOT, env_extra=None, check=True):
    env = [f'FACTORLAB_DEMO_STATE_ROOT={root}']
    for k, v in (env_extra or {}).items():
        env.append(f'{k}={v}')
    return run_cmd(['sudo', '-u', 'factorlab-demo', 'env', *env, *args], check=check)


def read_health(root=ROOT, unit=EXEC_UNIT):
    proc = as_service([PYTHON, '-m', CLOUD_MOD, 'health', '--unit', unit], root=root, check=False)
    return proc.returncode, json.loads(proc.stdout)


def read_snapshot(root=ROOT):
    proc = as_service([PYTHON, '-c', f"from pathlib import Path; print(Path('{root}/cloud-health.json').read_text())"], root=root)
    return json.loads(proc.stdout)


def wait_for(predicate, *, timeout=25, interval=0.3, description='condition'):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f'Timed out waiting for {description}; last={last!r}')


def write_sitecustomize(port):
    code = f'''
import urllib.request
from urllib.parse import urlsplit

_ORIG_BUILD_OPENER = urllib.request.build_opener
_STUB_BASE = 'http://127.0.0.1:{port}'


def _c04_build_opener(*handlers):
    opener = _ORIG_BUILD_OPENER(*handlers)
    orig_open = opener.open

    def _open(req, data=None, timeout=10):
        parsed = urlsplit(req.full_url)
        if parsed.netloc == 'demo-fapi.binance.com':
            target = _STUB_BASE + parsed.path + ('?' + parsed.query if parsed.query else '')
            proxied = urllib.request.Request(target, data=data, headers=dict(req.header_items()), method=req.get_method())
            return orig_open(proxied, timeout=timeout)
        return orig_open(req, data=data, timeout=timeout)

    opener.open = _open
    return opener


urllib.request.build_opener = _c04_build_opener
import factorlab.integrations.binance_futures_trading.protocol as _p
import factorlab.integrations.binance_futures_trading.network as _n
_p.build_opener = _c04_build_opener
_n.build_opener = _c04_build_opener
'''
    run_cmd(['sudo', 'mkdir', '-p', HARNESS_DIR])
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8') as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        run_cmd(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '644', tmp_path, f'{HARNESS_DIR}/sitecustomize.py'])
    finally:
        os.unlink(tmp_path)


def install_unit_from_repo(repo_unit_path, target_name, dropin_lines):
    run_cmd(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '644', str(repo_unit_path),
             f'/etc/systemd/system/{target_name}'])
    dropin_dir = f'/etc/systemd/system/{target_name}.d'
    run_cmd(['sudo', 'mkdir', '-p', dropin_dir])
    with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8') as tmp:
        tmp.write('\n'.join(dropin_lines) + '\n')
        tmp_path = tmp.name
    try:
        run_cmd(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '644', tmp_path,
                 f'{dropin_dir}/acceptance.conf'])
    finally:
        os.unlink(tmp_path)


def check_all_sqlite_integrity(root):
    proc = as_service([PYTHON, '-c', f'''
from contextlib import closing
from pathlib import Path
import json, sqlite3
root = Path('{root}')
results = {{}}
for path in sorted(root.rglob('*.sqlite3')):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        results[path.relative_to(root).as_posix()] = db.execute('PRAGMA integrity_check').fetchall()
print(json.dumps(results))
'''], root=root)
    results = json.loads(proc.stdout)
    assert results and all(val == [['ok']] for val in results.values()), results
    return results


def main():
    signal.alarm(240)
    repo_root = Path(__file__).resolve().parents[2]
    exec_template = repo_root / 'deploy' / 'systemd' / 'factorlab-demo-execute.service'
    get_template = repo_root / 'deploy' / 'systemd' / 'factorlab-demo.service'
    exchange = OfflineDemoExchange()
    server = start_stub_server(exchange)
    port = server.server_address[1]
    evidence = dict(
        started_at=datetime.now(timezone.utc).isoformat(),
        kernel=platform.release(),
        platform=platform.platform(),
        python=as_service([PYTHON, '--version'], root='/').stdout.strip() if False else run_cmd([PYTHON, '--version']).stdout.strip(),
        systemd=run_cmd(['systemctl', '--version']).stdout.splitlines()[0],
        mode='OFFLINE_LOOPBACK_SYSTEMD_EXECUTE',
        cloud='CLOUD_NOT_DEPLOYED',
        checks={},
    )

    try:
        # 0. Provision clean isolated state roots, secret, and acceptance units.
        for unit in (EXEC_UNIT, GET_UNIT, HALT_UNIT, CORRUPT_UNIT):
            systemctl('stop', unit, check=False)
        for directory in (ROOT, HALTED_ROOT, CORRUPT_ROOT):
            run_cmd(['sudo', 'rm', '-rf', directory])
            run_cmd(['sudo', 'install', '-d', '-o', 'factorlab-demo', '-g', 'factorlab-demo', '-m', '700', directory])
        run_cmd(['sudo', 'install', '-d', '-o', 'root', '-g', 'root', '-m', '700', '/etc/factorlab-demo'])

        with tempfile.NamedTemporaryFile('w', delete=False, encoding='utf-8') as tmp:
            json.dump(dict(environment='DEMO', key=KEY_SENTINEL, secret=SECRET_SENTINEL), tmp)
            tmp_secret = tmp.name
        try:
            run_cmd(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '600', tmp_secret, SECRET_PATH])
            # Temporary service-readable secret copy ONLY for setup/start-continuous, removed right after.
            setup_secret = f'{ROOT}/.setup-secret.json'
            run_cmd(['sudo', 'install', '-o', 'factorlab-demo', '-g', 'factorlab-demo', '-m', '600', tmp_secret, setup_secret])
        finally:
            os.unlink(tmp_secret)

        write_sitecustomize(port)

        # Verify unmodified production unit files pass systemd-analyze verify.
        run_cmd(['sudo', 'systemd-analyze', 'verify', str(exec_template)])
        run_cmd(['sudo', 'systemd-analyze', 'verify', str(get_template)])
        evidence['unit_verify'] = 'PASSED'

        install_unit_from_repo(exec_template, EXEC_UNIT, [
            '[Unit]',
            'Conflicts=',
            f'Conflicts={GET_UNIT}',
            'After=',
            f'After=network-online.target {GET_UNIT}',
            'RequiresMountsFor=',
            f'RequiresMountsFor={ROOT}',
            '[Service]',
            f'Environment=FACTORLAB_DEMO_STATE_ROOT={ROOT}',
            f'Environment=PYTHONPATH={HARNESS_DIR}',
            'LoadCredential=',
            f'LoadCredential=demo-secret:{SECRET_PATH}',
            'ExecStart=',
            f'ExecStart={DEMO_CLI} grid-supervisor start --execute --poll-seconds 0.5 --min-free-bytes 0',
            'RestartSec=2',
            'ReadWritePaths=',
            f'ReadWritePaths={ROOT}',
        ])
        install_unit_from_repo(get_template, GET_UNIT, [
            '[Unit]',
            'Conflicts=',
            f'Conflicts={EXEC_UNIT}',
            'After=',
            f'After=network-online.target {EXEC_UNIT}',
            'RequiresMountsFor=',
            f'RequiresMountsFor={ROOT}',
            '[Service]',
            f'Environment=FACTORLAB_DEMO_STATE_ROOT={ROOT}',
            f'Environment=PYTHONPATH={HARNESS_DIR}',
            'LoadCredential=',
            f'LoadCredential=demo-secret:{SECRET_PATH}',
            'ExecStart=',
            f'ExecStart={PYTHON} -m {CLOUD_MOD} run --poll-seconds 1 --min-free-bytes 0',
            'RestartSec=2',
            'ReadWritePaths=',
            f'ReadWritePaths={ROOT}',
        ])
        install_unit_from_repo(exec_template, HALT_UNIT, [
            '[Unit]',
            'Conflicts=',
            'After=network-online.target',
            'RequiresMountsFor=',
            f'RequiresMountsFor={HALTED_ROOT}',
            '[Service]',
            f'Environment=FACTORLAB_DEMO_STATE_ROOT={HALTED_ROOT}',
            f'Environment=PYTHONPATH={HARNESS_DIR}',
            'LoadCredential=',
            f'LoadCredential=demo-secret:{SECRET_PATH}',
            'ExecStart=',
            f'ExecStart={DEMO_CLI} grid-supervisor start --execute --poll-seconds 0.5 --min-free-bytes 0',
            'RestartSec=2',
            'ReadWritePaths=',
            f'ReadWritePaths={HALTED_ROOT}',
        ])
        install_unit_from_repo(exec_template, CORRUPT_UNIT, [
            '[Unit]',
            'Conflicts=',
            'After=network-online.target',
            'RequiresMountsFor=',
            f'RequiresMountsFor={CORRUPT_ROOT}',
            '[Service]',
            f'Environment=FACTORLAB_DEMO_STATE_ROOT={CORRUPT_ROOT}',
            f'Environment=PYTHONPATH={HARNESS_DIR}',
            'LoadCredential=',
            f'LoadCredential=demo-secret:{SECRET_PATH}',
            'ExecStart=',
            f'ExecStart={DEMO_CLI} grid-supervisor start --execute --poll-seconds 0.5 --min-free-bytes 0',
            'RestartSec=2',
            'ReadWritePaths=',
            f'ReadWritePaths={CORRUPT_ROOT}',
        ])
        systemctl('daemon-reload')

        # Bind account + initialize continuous authorization once (using production CLI).
        setup_env = dict(FACTORLAB_DEMO_SECRET_FILE=setup_secret, PYTHONPATH=HARNESS_DIR)
        setup_out = json.loads(as_service([DEMO_CLI, 'setup'], env_extra=setup_env).stdout)
        assert setup_out['status'] == 'DEMO_CONFIGURED', setup_out
        cont_out = json.loads(as_service(
            [DEMO_CLI, 'start-continuous', '--total-orders', '10', '--total-notional', '100',
             '--window-seconds', '3600', '--window-orders', '4'],
            env_extra=setup_env,
        ).stdout)
        assert cont_out['status'] == 'DEMO_CONTINUOUS_RUNNING', cont_out
        run_cmd(['sudo', 'rm', '-f', setup_secret])

        # ------------------------------------------------------------------
        # CHECK 1: RUNNING normal startup + 1 real Grid order + Health ready
        # ------------------------------------------------------------------
        exchange.align_next_bar(seconds_ahead=6)
        systemctl('start', EXEC_UNIT)
        health_running = wait_for(
            lambda: (res := read_health()) and (res[0] == 0 and res[1].get('demo_execution_ready') is True and res[1]),
            timeout=20, description='RUNNING healthy Execute service',
        )
        # Wait for first fresh 1m closed bar (~4s) to trigger 1 LIMIT BUY order.
        order1_health = wait_for(
            lambda: (snap := read_snapshot()) and snap.get('global_budget', {}).get('consumed', {}).get('posts', 0) >= 1 and snap,
            timeout=20, description='first Grid LIMIT order submission',
        )
        deployment_id = order1_health['grid']['deployment_id']
        order1_id = order1_health['recent_orders'][-1]['client_order_id']
        assert order1_health['mode'] == 'EXECUTE'
        assert order1_health['desired'] == 'RUNNING'
        assert order1_health['global_budget']['consumed']['posts'] == 1
        assert order1_health['global_budget']['remaining_posts'] == 9
        assert exchange.post_count == 1
        assert health_running['systemd']['active_state'] == 'active'
        evidence['checks']['1_running_normal_start'] = dict(
            passed=True, deployment_id=deployment_id, order_id=order1_id,
            posts_consumed=1, remaining_posts=9,
            systemd_active=health_running['systemd']['active_state'],
            demo_execution_ready=health_running['demo_execution_ready'],
        )

        # ------------------------------------------------------------------
        # CHECK 9 (part A): Dual-process lock contention (Execute + GET-only)
        # ------------------------------------------------------------------
        contender_exec = as_service(
            [DEMO_CLI, 'grid-supervisor', 'start', '--execute', '--seconds', '1', '--min-free-bytes', '0'],
            env_extra=dict(PYTHONPATH=HARNESS_DIR), check=False,
        )
        assert contender_exec.returncode == 78, (contender_exec.returncode, contender_exec.stdout, contender_exec.stderr)
        contender_get = as_service(
            [PYTHON, '-m', CLOUD_MOD, 'run', '--offline', '--seconds', '1', '--min-free-bytes', '0'],
            check=False,
        )
        assert contender_get.returncode == 78, (contender_get.returncode, contender_get.stdout, contender_get.stderr)

        # ------------------------------------------------------------------
        # CHECK 8 & 6 & 7: UNKNOWN order + SIGKILL crash -> restart same ledger,
        # GET resolves UNKNOWN with ZERO duplicate POST, budget preserved!
        # ------------------------------------------------------------------
        # Fill order #1 cleanly while service is stopped (no concurrent mid-poll race),
        # align next 1m bar ~6s ahead, arm timeout_next_post for reduce-only SELL (POST #2).
        systemctl('stop', EXEC_UNIT)
        exchange.fill_order(order1_id)
        exchange.timeout_next_post = True
        exchange.advance_toward_next_bar()
        systemctl('start', EXEC_UNIT)
        wait_for(
            lambda: systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'active',
            timeout=15, description='service active before UNKNOWN POST #2',
        )
        pid_before_kill = systemctl('show', EXEC_UNIT, '--property=MainPID', '--value')
        starts_before_kill = wait_for(
            lambda: (snap := read_snapshot()) and str(snap.get('pid')) == pid_before_kill and snap['starts'],
            timeout=15, description='snapshot for pid_before_kill',
        )
        # Wait until stub receives POST #2 (reduce-only SELL) whose reply is dropped -> UNKNOWN.
        wait_for(lambda: exchange.post_count == 2, timeout=65, description='2nd POST (UNKNOWN timeout)')
        # Give worker a moment to persist UNKNOWN record, then immediately SIGKILL!
        time.sleep(0.3)
        systemctl('kill', '--signal=SIGKILL', '--kill-whom=main', EXEC_UNIT)

        after_kill = wait_for(
            lambda: (
                systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'active'
                and systemctl('show', EXEC_UNIT, '--property=MainPID', '--value') not in {'0', pid_before_kill}
                and (res := read_health())
                and res[0] == 0
                and res[1].get('account_recovered') is True
                and res[1].get('unknown_count') == 0
                and res[1]
            ),
            timeout=25, description='post-SIGKILL restart and UNKNOWN GET recovery',
        )
        assert after_kill['grid']['deployment_id'] == deployment_id
        assert after_kill['starts'] == starts_before_kill + 1
        assert int(systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value')) >= 1
        # CRITICAL: ZERO duplicate POST for the UNKNOWN order, budget stays exact (2 consumed, 8 remaining)!
        assert exchange.post_count == 2
        assert after_kill['global_budget']['consumed']['posts'] == 2
        assert after_kill['global_budget']['remaining_posts'] == 8
        assert len(after_kill['recent_orders']) == 2
        evidence['checks']['6_sigkill_same_ledger'] = dict(
            passed=True, old_pid=pid_before_kill, new_pid=str(after_kill['pid']),
            n_restarts=int(systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value')),
            deployment_id=deployment_id, starts=after_kill['starts'],
        )
        evidence['checks']['8_unknown_no_resubmit'] = dict(
            passed=True, exchange_post_count=exchange.post_count,
            unknown_count_after_recovery=after_kill['unknown_count'],
            orders_count=len(after_kill['recent_orders']),
        )

        # ------------------------------------------------------------------
        # CHECK 4: PAUSED is NOT overwritten across restart
        # ------------------------------------------------------------------
        pause_out = json.loads(as_service([DEMO_CLI, 'grid-supervisor', 'pause', '--execute']).stdout)
        assert pause_out['desired'] == 'PAUSED'
        systemctl('restart', EXEC_UNIT)
        paused_health = wait_for(
            lambda: (res := read_health()) and res[0] == 1 and res[1].get('status') == 'PAUSED' and res[1].get('desired') == 'PAUSED' and res[1],
            timeout=15, description='PAUSED state preserved after systemd restart',
        )
        assert paused_health['demo_execution_ready'] is False
        assert paused_health['systemd']['active_state'] == 'active'
        assert exchange.post_count == 2
        # Resume back to RUNNING cleanly.
        resume_out = json.loads(as_service([DEMO_CLI, 'grid-supervisor', 'resume', '--execute']).stdout)
        assert resume_out['desired'] == 'RUNNING'
        wait_for(
            lambda: (res := read_health()) and res[0] == 0 and res[1].get('status') == 'RUNNING',
            timeout=15, description='resume from PAUSED back to RUNNING',
        )
        evidence['checks']['4_paused_preserved'] = dict(
            passed=True, desired_after_restart='PAUSED',
            ready_while_paused=paused_health['demo_execution_ready'],
            resumed_to='RUNNING',
        )

        # ------------------------------------------------------------------
        # CHECK 5: Startup network outage -> Supervisor backoff -> Auto recovery
        # ------------------------------------------------------------------
        systemctl('stop', EXEC_UNIT)
        assert systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', EXEC_UNIT, '--property=ExecMainStatus', '--value') == '0'
        exchange.outage = True
        systemctl('start', EXEC_UNIT)
        outage_health = wait_for(
            lambda: (res := read_health()) and res[0] == 1 and res[1].get('status') == 'RECOVERING'
                    and (res[1].get('recovery') or {}).get('recovery_attempts', 0) >= 1 and res[1],
            timeout=15, description='startup outage entering RECOVERING state',
        )
        outage_pid = systemctl('show', EXEC_UNIT, '--property=MainPID', '--value')
        assert outage_health['systemd']['active_state'] == 'active'
        assert outage_health['demo_execution_ready'] is False
        assert outage_health['account_recovered'] is False
        assert int(systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value')) == 0
        # Restore network -> service must recover in-place (same PID, NRestarts=0, no duplicate Grid/Campaign).
        exchange.outage = False
        recovered_health = wait_for(
            lambda: (res := read_health()) and res[0] == 0 and res[1].get('demo_execution_ready') is True
                    and (res[1].get('recovery') or {}).get('state') == 'RECOVERED' and res[1],
            timeout=25, description='automatic recovery after startup network outage',
        )
        assert systemctl('show', EXEC_UNIT, '--property=MainPID', '--value') == outage_pid
        assert int(systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value')) == 0
        assert recovered_health['grid']['deployment_id'] == deployment_id
        assert recovered_health['global_budget']['consumed']['posts'] == 2
        assert exchange.post_count == 2
        evidence['checks']['5_startup_outage_auto_recovery'] = dict(
            passed=True, pid_unchanged=True, n_restarts=0,
            outage_attempts=outage_health['recovery']['recovery_attempts'],
            recovered_state=recovered_health['recovery']['state'],
            deployment_id=recovered_health['grid']['deployment_id'],
        )

        # ------------------------------------------------------------------
        # CHECK 9 (part B): systemd Conflicts= switchover GET-only <-> Execute
        # ------------------------------------------------------------------
        systemctl('start', GET_UNIT)
        wait_for(
            lambda: systemctl('show', GET_UNIT, '--property=ActiveState', '--value') == 'active'
                    and systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'inactive',
            timeout=15, description='systemd switchover Execute -> GET-only',
        )
        systemctl('start', EXEC_UNIT)
        wait_for(
            lambda: systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'active'
                    and systemctl('show', GET_UNIT, '--property=ActiveState', '--value') == 'inactive',
            timeout=15, description='systemd switchover GET-only -> Execute',
        )
        evidence['checks']['9_dual_process_and_switchover'] = dict(
            passed=True, second_execute_exit=contender_exec.returncode,
            concurrent_get_only_exit=contender_get.returncode,
            systemd_conflicts_switchover=True,
        )

        # Finish the existing reduction, deliver a breakout bar, then recover
        # the replacement and its first working order in a separate process.
        order2_id = next(client_id for client_id in exchange.orders if client_id != order1_id)
        systemctl('stop', EXEC_UNIT)
        exchange.fill_order(order2_id)
        with exchange.lock:
            exchange.breakout_bar_open = exchange.server_ms() // 60_000 * 60_000
        systemctl('start', EXEC_UNIT)
        replacement_id = deployment_id + '-00001'
        rotated = wait_for(
            lambda: (snap := read_snapshot()) and (snap.get('grid') or {}).get('deployment_id') == replacement_id
                    and snap['global_budget']['consumed']['posts'] == 2 and snap,
            timeout=80, description='flat BREAKOUT rotated under systemd',
        )
        assert not rotated['grid']['live_bar']
        replacement_order_id = replacement_id + '-00000001'
        working = wait_for(
            lambda: (snap := read_snapshot()) and any(
                order['client_order_id'] == replacement_order_id for order in snap.get('open_orders') or [])
                and snap,
            timeout=80, description='first fresh bar submits under rotated Grid',
        )
        assert working['global_budget']['consumed']['posts'] == 3
        assert exchange.post_count == 3 and exchange.delete_count == 0
        systemctl('restart', EXEC_UNIT)
        resumed = wait_for(
            lambda: (res := read_health()) and res[0] == 0 and res[1]['pid'] != working['pid']
                    and res[1]['grid']['deployment_id'] == replacement_id and res[1],
            timeout=25, description='independent systemd process recovers rotated Grid and working order',
        )
        assert resumed['global_budget'] == working['global_budget']
        assert [order['client_order_id'] for order in resumed['open_orders']] == [replacement_order_id]
        assert resumed['recent_orders'] == working['recent_orders']
        assert exchange.post_count == 3 and exchange.delete_count == 0
        evidence['checks']['f08_rotation_and_restart'] = dict(
            passed=True, previous_deployment_id=deployment_id, deployment_id=replacement_id,
            client_order_id=replacement_order_id, old_pid=working['pid'], new_pid=resumed['pid'],
            consumed=resumed['global_budget']['consumed'],
        )

        # ------------------------------------------------------------------
        # CHECK 2: Durable STOP (both worker STOP and account-wide STOP)
        # ------------------------------------------------------------------
        as_service([DEMO_CLI, 'grid-supervisor', 'stop', '--execute'])
        wait_for(
            lambda: systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'inactive',
            timeout=15, description='natural exit after worker STOP',
        )
        assert systemctl('show', EXEC_UNIT, '--property=ExecMainStatus', '--value') == '0'
        gets_before_stop_start = exchange.get_count
        posts_before_stop_start = exchange.post_count
        systemctl('start', '--wait', EXEC_UNIT)
        assert systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', EXEC_UNIT, '--property=ExecMainStatus', '--value') == '0'
        assert systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value') == '0'
        assert exchange.get_count == gets_before_stop_start
        assert exchange.post_count == posts_before_stop_start
        worker_stop_code, worker_stop_health = read_health()
        assert worker_stop_code == 1
        assert worker_stop_health['desired'] == 'STOPPED'
        assert worker_stop_health['status'] == 'STOPPED'
        assert worker_stop_health['demo_execution_ready'] is False

        # Resume worker control, then apply account-wide `factorlab-demo stop`.
        as_service([DEMO_CLI, 'grid-supervisor', 'resume', '--execute'])
        as_service([DEMO_CLI, 'stop'])
        systemctl('start', '--wait', EXEC_UNIT)
        assert systemctl('show', EXEC_UNIT, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', EXEC_UNIT, '--property=ExecMainStatus', '--value') == '0'
        assert systemctl('show', EXEC_UNIT, '--property=NRestarts', '--value') == '0'
        assert exchange.get_count == gets_before_stop_start
        assert exchange.post_count == posts_before_stop_start
        acct_stop_code, acct_stop_health = read_health()
        assert acct_stop_code == 1
        assert acct_stop_health['desired'] == 'STOPPED'
        assert acct_stop_health['global_budget']['consumed']['posts'] == 3
        assert acct_stop_health['global_budget']['remaining_posts'] == 7
        evidence['checks']['2_durable_stop_preserved'] = dict(
            passed=True, worker_stop_exit=0, account_stop_exit=0,
            zero_network_calls_while_stopped=True, n_restarts=0,
        )
        evidence['checks']['7_budget_not_reset_or_double_counted'] = dict(
            passed=True, consumed=acct_stop_health['global_budget']['consumed'],
            remaining_posts=acct_stop_health['global_budget']['remaining_posts'],
            exchange_posts=exchange.post_count,
        )

        # ------------------------------------------------------------------
        # CHECK 3: Durable HALTED (isolated cloned root, never un-halted)
        # ------------------------------------------------------------------
        run_cmd(['sudo', 'cp', '-a', f'{ROOT}/.', HALTED_ROOT])
        run_cmd(['sudo', 'chown', '-R', 'factorlab-demo:factorlab-demo', HALTED_ROOT])
        as_service([PYTHON, '-c', f'''
from contextlib import closing
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime, state_root
with closing(DemoRuntime(state_root())) as runtime:
    auth = runtime.continuous_authorization()
    auth['state'] = 'RUNNING'
    with runtime.db:
        runtime._set('continuous', __import__('json').dumps(auth))
    runtime.halt('c04 acceptance risk halt')
'''], root=HALTED_ROOT)
        gets_before_halt = exchange.get_count
        posts_before_halt = exchange.post_count
        systemctl('start', '--wait', HALT_UNIT)
        assert systemctl('show', HALT_UNIT, '--property=ActiveState', '--value') == 'inactive'
        assert systemctl('show', HALT_UNIT, '--property=ExecMainStatus', '--value') == '0'
        assert systemctl('show', HALT_UNIT, '--property=NRestarts', '--value') == '0'
        assert exchange.get_count == gets_before_halt
        assert exchange.post_count == posts_before_halt
        halt_code, halt_health = read_health(root=HALTED_ROOT, unit=HALT_UNIT)
        assert halt_code == 1
        assert halt_health['status'] == 'HALTED'
        assert halt_health['desired'] == 'HALTED'
        assert halt_health['diagnostic'] is not None
        assert halt_health['demo_execution_ready'] is False
        assert halt_health['global_budget']['consumed']['posts'] == 3
        evidence['checks']['3_durable_halted_preserved'] = dict(
            passed=True, exit_status=0, n_restarts=0,
            status=halt_health['status'], diagnostic=halt_health['diagnostic'],
        )

        # ------------------------------------------------------------------
        # BONUS CHECK: Corrupt SQLite -> explicit exit 78 & no restart loop
        # ------------------------------------------------------------------
        as_service([PYTHON, '-c', f"from pathlib import Path; Path('{CORRUPT_ROOT}/runtime.sqlite3').write_bytes(b'corrupt')"],
                   root=CORRUPT_ROOT)
        systemctl('start', CORRUPT_UNIT, check=False)
        wait_for(
            lambda: systemctl('show', CORRUPT_UNIT, '--property=ActiveState', '--value') == 'failed',
            timeout=15, description='corrupt SQLite unit failed with exit 78',
        )
        assert systemctl('show', CORRUPT_UNIT, '--property=ExecMainStatus', '--value') == '78'
        assert systemctl('show', CORRUPT_UNIT, '--property=NRestarts', '--value') == '0'
        evidence['checks']['bonus_corrupt_sqlite_exit_78'] = dict(
            passed=True, exec_main_status=78, n_restarts=0,
        )

        # SQLite integrity on main acceptance root
        evidence['sqlite_integrity'] = check_all_sqlite_integrity(ROOT)

        # ------------------------------------------------------------------
        # CHECK 10: Zero credential leakage in journald, health JSON, and DBs
        # ------------------------------------------------------------------
        logs = run_cmd(['sudo', 'journalctl', '--no-pager', '-u', EXEC_UNIT, '-u', HALT_UNIT, '-u', CORRUPT_UNIT]).stdout
        health_raw = as_service([PYTHON, '-c', f"from pathlib import Path; print(Path('{ROOT}/cloud-health.json').read_text())"]).stdout
        db_dump = as_service([PYTHON, '-c', f'''
from contextlib import closing
from pathlib import Path
import sqlite3
chunks = []
for name in ('runtime.sqlite3', 'grid-supervisor.sqlite3'):
    p = Path('{ROOT}') / name
    with closing(sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)) as db:
        for (tbl,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in db.execute(f'SELECT * FROM "{{tbl}}"'):
                chunks.append(repr(row))
print('\\n'.join(chunks))
''']).stdout
        for name, blob in [('journalctl', logs), ('cloud-health.json', health_raw), ('sqlite_rows', db_dump)]:
            assert KEY_SENTINEL not in blob, f'API key leaked in {name}!'
            assert SECRET_SENTINEL not in blob, f'API secret leaked in {name}!'
        evidence['checks']['10_no_credential_leakage'] = dict(
            passed=True, scanned=['journalctl', 'cloud-health.json', 'runtime.sqlite3', 'grid-supervisor.sqlite3'],
        )
        evidence['ended_at'] = datetime.now(timezone.utc).isoformat()
        evidence['all_passed'] = True
        print(json.dumps(evidence, indent=2))
    finally:
        if os.environ.get('FACTORLAB_C04_EVIDENCE_OUT'):
            Path(os.environ['FACTORLAB_C04_EVIDENCE_OUT']).write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        server.shutdown()
        server.server_close()
        for unit in (EXEC_UNIT, GET_UNIT, HALT_UNIT, CORRUPT_UNIT):
            systemctl('stop', unit, check=False)
            run_cmd(['sudo', 'rm', '-rf', f'/etc/systemd/system/{unit}', f'/etc/systemd/system/{unit}.d'], check=False)
        systemctl('daemon-reload', check=False)
        for path in (ROOT, HALTED_ROOT, CORRUPT_ROOT, SECRET_PATH, HARNESS_DIR):
            run_cmd(['sudo', 'rm', '-rf', path], check=False)


if __name__ == '__main__':
    main()
