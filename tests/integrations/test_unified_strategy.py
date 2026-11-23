"""Offline protocol -> worker -> controller -> Demo REST -> journal regression."""
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal as D
import json
import os
from pathlib import Path

import pytest

from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError
from factorlab.strategies.decision_runtime import DecisionJournal, SignalAdapter, OrderStrategyRuntime
from factorlab.strategies.protocol import Decision
from factorlab.trading.strategy import ClosedBar, DualMAStrategy
from factorlab.trading.paper import Quote, PaperBroker, Instrument
from factorlab.trading.storage import PaperStore
from factorlab.trading.contracts import Market, OrderStatus, Side
from factorlab.trading.control import TradingController, RiskLimits
from factorlab.trading.realtime import PersistenceConflict
from factorlab.integrations.binance_futures_trading.strategy_runtime import DemoStrategyRuntime
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.research.workflow import run_protocol_research
from test_binance_demo_execution import setup
from test_demo_strategy_supervisor import worker_composition, deployment, wired

ROOT = Path(__file__).resolve().parents[2]


def manifest(tmp_path, source, cls, version='1', parameters=None, package=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / 'strategy.py').write_text(source, encoding='utf-8')
    (tmp_path / 'strategy.json').write_text(json.dumps(dict(file='strategy.py',
        **{'class': cls}, version=version, parameters=parameters or {}, package=package)), encoding='utf-8')
    return LoadedStrategy(tmp_path, trusted=True)


def custom(tmp_path, interval=60):
    return manifest(tmp_path, (ROOT / 'examples/protocol_strategy/strategy.py').read_text(),
        'CloseMomentum', '1.0.0', {'interval_seconds': interval})


def journal(strategy, controller, deployment='custom', interval=60):
    return DecisionJournal(strategy, controller, deployment_id=deployment, symbol='NVDAUSDT',
        interval_seconds=interval, data_types=['closed_bar', 'quote', 'order_event'])


def bar(i, price, interval=60):
    opened = NOW - timedelta(seconds=(22 - i) * interval)
    p = D(price)
    return ClosedBar('NVDAUSDT', opened, opened + timedelta(seconds=interval),
        p, p, p, p, D(100), interval, 'BINANCE_USDM_PUBLIC_REST')


def test_custom_demo_fill_restart_and_source_binding(tmp_path):
    strategy = custom(tmp_path / 'strategy')
    a, g, c, fake = setup(tmp_path / 'protocol.db', tmp_path / 'account.db')
    j = journal(strategy, c)
    runner = DemoStrategyRuntime(c, SignalAdapter(j))
    runner.recover()
    runner.closed_bar(bar(20, '100'))
    runner.closed_bar(bar(21, '110'))
    assert j._latest() == {'decisions': 2}
    at = NOW + timedelta(milliseconds=1)
    c.clock = g.clock = lambda: at
    event = runner.quote(Quote('NVDAUSDT', D(100), at))
    assert event.status is OrderStatus.SUBMITTED
    intent = g.state.orders[event.client_order_id].snapshot.intent
    fake.trade_update(event.client_order_id, str(intent.quantity))
    runner.recover()
    assert c.account().positions[0].quantity == intent.quantity
    before = len([x for x in fake.calls if x[0] == 'POST'])
    a.store.close()
    c.store.close()
    a, g, c, fake = setup(tmp_path / 'protocol.db', tmp_path / 'account.db', fake, now=at)
    restored = journal(strategy, c)
    runner = DemoStrategyRuntime(c, SignalAdapter(restored))
    runner.recover()
    assert not runner.closed_bar(bar(21, '110'))
    assert restored._latest() == {'decisions': 2}
    assert runner.quote(Quote('NVDAUSDT', D(100), at)) is None
    assert before == len([x for x in fake.calls if x[0] == 'POST'])
    assert restored._count('restore:') == 2
    changed = custom(tmp_path / 'changed', interval=3600)
    with pytest.raises((PersistenceConflict, ValueError)):
        journal(changed, c)


def test_dualma_worker_matches_native_and_demo(tmp_path):
    strategy = manifest(tmp_path / 'ma',
        'from factorlab.strategies.adapters import DualMAAdapter\n', 'DualMAAdapter',
        parameters={'interval_seconds': 60})
    a, g, c, fake = setup()
    adapter = SignalAdapter(journal(strategy, c))
    runner = DemoStrategyRuntime(c, adapter)
    runner.recover()
    history = []
    for i in range(21):
        b = bar(i + 1, '110' if i == 20 else '100')
        history.append(b)
        runner.closed_bar(b)
    expected = DualMAStrategy(interval_seconds=60).signal(history, NOW)
    signal = adapter.signal(history, NOW)
    assert (signal.direction, signal.target_weight) == (expected.direction, expected.target_weight)
    at = NOW + timedelta(milliseconds=1)
    c.clock = g.clock = lambda: at
    assert runner.quote(Quote('NVDAUSDT', D(100), at)).status is OrderStatus.SUBMITTED
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1


def grid_strategy(path, account_id):
    return manifest(path, 'from factorlab.strategies.adapters import GridAdapter\n', 'GridAdapter',
        parameters=dict(config=dict(lower=145, upper=155, levels=1, initial_price=150,
            initial_inventory=0, quantity=1, max_inventory=1, max_cash_committed=7,
            breakout='pause'), rules=dict(quantity_unit='.04', step_size='.01', tick_size='.01',
            min_quantity='.01', min_notional='5'), deployment_id='grid-demo', account_id=account_id,
            market='BINANCE_USDM', instrument_id='NVDAUSDT', initial_equity='1000', interval_seconds=60))


def test_grid_protocol_preserves_limit_lifecycle_and_restart(tmp_path):
    a, g, c, fake = setup(tmp_path / 'protocol.db', tmp_path / 'account.db')
    strategy = grid_strategy(tmp_path / 'strategy', a.account_id)
    c.quote(Quote('NVDAUSDT', D(150), NOW))
    runner = OrderStrategyRuntime(journal(strategy, c, 'grid-demo'))
    runner.recover()
    assert runner.quote(Quote('NVDAUSDT', D(150), NOW)).status is OrderStatus.SUBMITTED
    buy = next(iter(g.state.orders.values())).snapshot.intent
    assert buy.limit_price == D(145) and buy.quantity == D('.04')
    fake.trade_update(buy.client_order_id, '.02', '145')
    runner.recover()
    assert c.account().positions[0].quantity == D('.02')
    a.store.close()
    c.store.close()
    a, g, c, fake = setup(tmp_path / 'protocol.db', tmp_path / 'account.db', fake)
    runner = OrderStrategyRuntime(journal(strategy, c, 'grid-demo'))
    runner.recover()
    fake.trade_update(buy.client_order_id, '.02', '145')
    runner.recover()
    at = NOW + timedelta(milliseconds=1)
    c.clock = g.clock = lambda: at
    assert runner.quote(Quote('NVDAUSDT', D(150), at)).status is OrderStatus.SUBMITTED
    sell = next(o.snapshot.intent for o in g.state.orders.values() if o.snapshot.intent.side is Side.SELL)
    assert sell.limit_price == D(155) and sell.quantity == D('.04') and sell.reduce_only
    assert len([x for x in fake.calls if x[0] == 'POST']) == 2
    runner.quote(Quote('NVDAUSDT', D(150), at))
    assert len([x for x in fake.calls if x[0] == 'POST']) == 2
    runner.recover()
    assert len([x for x in fake.calls if x[0] == 'POST']) == 2


@pytest.mark.parametrize('body', [
    "open(SECRET_PATH).read()",
    "__import__('socket').socket().connect(('127.0.0.1', 443))",
    "__import__('subprocess').run(['whoami'])",
])
def test_worker_external_io_blocked(tmp_path, monkeypatch, body):
    secret = tmp_path / 'credentials.txt'
    secret.write_text('sentinel-key')
    monkeypatch.setenv('BINANCE_API_SECRET', 'sentinel-env-key')
    source = '''import os
from factorlab.strategies.protocol import StrategySpec, Decision
class Probe:
    def describe(self):
        return StrategySpec('probe', '1', 'target', ['NVDAUSDT'], [60], ['closed_bar'])
    def decide(self, event, state):
        assert 'BINANCE_API_SECRET' not in os.environ
        BODY
        return Decision()
'''.replace('BODY', body.replace('SECRET_PATH', repr(str(secret))))
    strategy = manifest(tmp_path / 'strategy', source, 'Probe')
    with pytest.raises(StrategyWorkerError):
        strategy.decide(dict(symbol='NVDAUSDT'), {})


def test_package_hash_relative_import_and_explicit_trust(tmp_path):
    (tmp_path / 'helper.py').write_text('VERSION = "1"')
    strategy = manifest(tmp_path, '''from .helper import VERSION
from factorlab.strategies.protocol import StrategySpec, Decision
class Package:
    def describe(self):
        return StrategySpec('package', VERSION, 'target', ['OTHER'], [300], ['closed_bar'])
    def decide(self, event, state):
        return Decision(state={'ok': True})
''', 'Package', package=True)
    with pytest.raises(ValueError, match='trusted'):
        LoadedStrategy(tmp_path)
    strategy.spec.require(symbol='OTHER', interval_seconds=300, data_types=['closed_bar'])
    config = json.loads((tmp_path / 'strategy.json').read_text())
    config['sha256'] = strategy.code_sha256
    (tmp_path / 'strategy.json').write_text(json.dumps(config))
    (tmp_path / 'helper.py').write_text('VERSION = "2"')
    with pytest.raises(ValueError, match='code identity'):
        LoadedStrategy(tmp_path, trusted=True)


def test_research_custom_uses_same_decisions_and_tracker(tmp_path):
    strategy = custom(tmp_path / 'strategy')
    broker = PaperBroker(Market.BINANCE_USDM, 'research', (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D(1000)})
    c = TradingController(broker, PaperStore(tmp_path / 'research.db'), clock=lambda: NOW,
        limits=RiskLimits(D(500), D(500), D(800), 10))
    c.quote(Quote('NVDAUSDT', D(100), NOW))
    events = [('closed_bar', bar(20, '100')), ('closed_bar', bar(21, '110'))]
    at = NOW + timedelta(milliseconds=1)
    def stream():
        yield from events
        c.clock = broker.clock = lambda: at
        yield 'quote', Quote('NVDAUSDT', D(100), at)
        order = next(iter(broker.state.orders.values())).snapshot.intent
        yield 'fill', dict(order_id=order.client_order_id, quantity=order.quantity, price=D(100))
    runner = run_protocol_research(strategy, c, stream(), symbol='NVDAUSDT', interval_seconds=60,
        deployment_id='custom', store=tmp_path / 'tracker', experiment_id='custom', run_id='offline')
    assert c.account().positions[0].quantity == D('.09')
    from factorlab.experiments.tracker import ExperimentTracker
    receipt = ExperimentTracker(tmp_path / 'tracker').get_run('offline')
    assert receipt['payload']['run']['status'] == 'SUCCEEDED'
    assert runner.journal._latest() == {'decisions': 2}


@pytest.mark.parametrize('kind', ['grid', 'dual_ma'])
def test_native_adapters_research_round_trip(tmp_path, kind):
    broker = PaperBroker(Market.BINANCE_USDM, 'research', (Instrument('NVDAUSDT', 'USDT'),), {'USDT': D(1000)})
    c = TradingController(broker, PaperStore(tmp_path / 'research.db'), clock=lambda: NOW,
        limits=RiskLimits(D(500), D(500), D(800), 20))
    strategy = (grid_strategy(tmp_path / 'strategy', 'research') if kind == 'grid' else
        manifest(tmp_path / 'strategy', 'from factorlab.strategies.adapters import DualMAAdapter\n',
                 'DualMAAdapter', parameters={'interval_seconds': 60}))
    deployment = 'grid-demo' if kind == 'grid' else 'dual-ma'
    def stream():
        if kind == 'dual_ma':
            for i in range(21):
                yield 'closed_bar', bar(i + 1, '110' if i == 20 else '100')
        at = NOW + timedelta(milliseconds=1)
        c.clock = broker.clock = lambda: at
        yield 'quote', Quote('NVDAUSDT', D(150), at)
        buy = next(o.snapshot.intent for o in broker.state.orders.values() if o.snapshot.intent.side is Side.BUY)
        yield 'fill', dict(order_id=buy.client_order_id, quantity=buy.quantity, price=buy.limit_price)
        if kind == 'grid':
            at += timedelta(milliseconds=1)
            yield 'quote', Quote('NVDAUSDT', D(150), at)
            sell = next(o.snapshot.intent for o in broker.state.orders.values() if o.snapshot.intent.side is Side.SELL)
            assert sell.limit_price == D(155) and sell.reduce_only
            yield 'fill', dict(order_id=sell.client_order_id, quantity=sell.quantity, price=sell.limit_price)
    run_protocol_research(strategy, c, stream(), symbol='NVDAUSDT', interval_seconds=60,
        deployment_id=deployment, store=tmp_path / 'tracker', experiment_id=kind, run_id='offline')
    if kind == 'grid':
        assert sum((p.quantity for p in c.account().positions), D(0)) == 0
        assert len(c.broker.state.orders) == 2
    else:
        assert c.account().positions[0].quantity > 0


def test_capability_mismatch_and_worker_timeout(tmp_path):
    strategy = custom(tmp_path / 'strategy')
    a, g, c, fake = setup()
    with pytest.raises(ValueError, match='requirements'):
        journal(strategy, c, interval=300)
    with pytest.raises(ValueError, match='requirements'):
        strategy.spec.require(symbol='OTHER', interval_seconds=60, data_types=['closed_bar'])
    source = (ROOT / 'examples/protocol_strategy/strategy.py').read_text().replace(
        "bars = event['bars']", "while True: pass")
    strategy = manifest(tmp_path / 'loop', source, 'CloseMomentum', '1.0.0')
    strategy.timeout = .3
    with pytest.raises(StrategyWorkerError, match='timed out'):
        strategy.decide(dict(symbol='NVDAUSDT'), {})
    assert not [x for x in fake.calls if x[0] != 'GET']


def test_direct_python_file_and_invalid_decision_rollback(tmp_path):
    strategy = custom(tmp_path / 'strategy')
    direct = LoadedStrategy(tmp_path / 'strategy/strategy.py', trusted=True,
        class_name='CloseMomentum', version='1.0.0', parameters={'interval_seconds': 60})
    assert direct.code_sha256 == strategy.code_sha256
    a, g, c, fake = setup()
    source = (ROOT / 'examples/protocol_strategy/strategy.py').read_text().replace(
        'return Decision(state=state)', "return Decision(target_weight=Decimal('2'), state=state)")
    bad = manifest(tmp_path / 'bad', source, 'CloseMomentum', '1.0.0')
    j = journal(bad, c)
    with pytest.raises(StrategyWorkerError, match='target_weight'):
        SignalAdapter(j).signal([bar(21, '110')], NOW)
    assert j._latest() == {}
    assert not [x for x in fake.calls if x[0] != 'GET']


def test_arbitrary_order_strategy_factory_fill_and_budget(tmp_path):
    from factorlab.integrations.binance_futures_trading.unified_strategy import strategy_runtime
    strategy = LoadedStrategy(ROOT / 'examples/protocol_strategy/limit_once.json', trusted=True)
    a, g, c, fake = setup()
    runner = strategy_runtime(c, strategy=strategy, interval_seconds=60, deployment_id='custom',
        client_id_prefix='custom-', max_quantity=D('.04'), max_notional=D(7))
    runner.recover()
    assert runner.quote(Quote('NVDAUSDT', D(150), NOW)).status is OrderStatus.SUBMITTED
    assert runner.quote(Quote('NVDAUSDT', D(150), NOW)) is None
    fake.trade_update('custom-entry', '.04', '145')
    runner.recover()
    assert c.account().positions[0].quantity == D('.04')
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1
    # A fresh adapter cannot change the persisted execution budget.
    with pytest.raises(PersistenceConflict, match='configuration'):
        strategy_runtime(c, strategy=strategy, interval_seconds=60, deployment_id='custom',
            client_id_prefix='custom-', max_quantity=D('.08'), max_notional=D(7))


def test_worker_process_and_environment_separation(tmp_path, monkeypatch):
    monkeypatch.setenv('BINANCE_API_SECRET', 'sentinel')
    strategy = manifest(tmp_path, '''import os
from factorlab.strategies.protocol import StrategySpec, Decision
class Probe:
    def describe(self):
        return StrategySpec('probe', '1', 'target', ['NVDAUSDT'], [60], ['closed_bar'])
    def decide(self, event, state):
        return Decision(state={'pid': os.getpid(), 'has_secret': 'BINANCE_API_SECRET' in os.environ})
''', 'Probe')
    decision = strategy.decide(dict(symbol='NVDAUSDT'), {})
    assert decision.state['pid'] != os.getpid()
    assert decision.state['has_secret'] is False


def test_order_decision_and_pending_checkpoint_commit_together(tmp_path, monkeypatch):
    strategy = LoadedStrategy(ROOT / 'examples/protocol_strategy/limit_once.json', trusted=True)
    a, g, c, fake = setup()
    j = journal(strategy, c)
    runner = OrderStrategyRuntime(j)
    runner.recover()
    pending = runner._pending
    def crash(*args):
        raise RuntimeError('interrupted before pending checkpoint')
    monkeypatch.setattr(runner, '_pending', crash)
    with pytest.raises(RuntimeError, match='interrupted'):
        runner.quote(Quote('NVDAUSDT', D(150), NOW))
    assert j._latest() == {} and g.state.runtime['pending'] is None
    assert not [x for x in fake.calls if x[0] != 'GET']
    monkeypatch.setattr(runner, '_pending', pending)
    assert runner.quote(Quote('NVDAUSDT', D(150), NOW)).status is OrderStatus.SUBMITTED


def test_grid_cancellation_and_uncertain_submit_use_existing_gateway(tmp_path):
    strategy_root = tmp_path / 'strategy'
    a, g, c, fake = setup()
    strategy = grid_strategy(strategy_root, a.account_id)
    config = json.loads((strategy_root / 'strategy.json').read_text())
    config['parameters']['config']['order_ttl'] = 1
    (strategy_root / 'strategy.json').write_text(json.dumps(config))
    strategy = LoadedStrategy(strategy_root, trusted=True)
    runner = OrderStrategyRuntime(journal(strategy, c, 'grid-demo'))
    runner.recover()
    fake.timeout_next = True
    assert runner.quote(Quote('NVDAUSDT', D(150), NOW)) is None
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1
    runner.recover()
    at = NOW + timedelta(milliseconds=1)
    c.clock = g.clock = lambda: at
    runner.quote(Quote('NVDAUSDT', D(150), at))
    runner.recover()
    assert len([x for x in fake.calls if x[0] == 'POST']) == 1
    assert len([x for x in fake.calls if x[0] == 'DELETE']) == 1
    assert next(iter(g.state.orders.values())).snapshot.status is OrderStatus.CANCELED


def test_outbox_gap_blocks_resubmission_and_risk_stays_authoritative(tmp_path):
    from factorlab.strategies.decision_runtime import ControllerExecution
    from factorlab.trading.contracts import OrderIntent, OrderType
    from factorlab.trading.control import RiskRejected
    a, g, c, fake = setup()
    c.quote(Quote('NVDAUSDT', D(150), NOW))
    intent = OrderIntent(Market.BINANCE_USDM, 'NVDAUSDT', Side.BUY, OrderType.LIMIT,
        D('.04'), D(145), 'gap-entry', position_side='BOTH')
    execution = ControllerExecution(c, deployment_id='gap')
    c.set_kill_switch(True)
    with pytest.raises(RiskRejected):
        execution.dispatch(Decision(orders=(intent,)))
    c.set_kill_switch(False)
    with c.store.transaction():
        c.store.record_metadata(execution.prefix + intent.client_order_id, dict(intent=intent, at=NOW))
    with pytest.raises(PersistenceConflict, match='unresolved'):
        execution.dispatch(Decision(orders=(intent,)))
    assert not [x for x in fake.calls if x[0] != 'GET']


@pytest.mark.parametrize('execute', [False, True])
def test_supervisor_loads_custom_strategy(worker_composition, monkeypatch, tmp_path, execute):
    from contextlib import closing
    from types import SimpleNamespace
    from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
    module, root, store, campaign, fake, market = worker_composition
    custom(tmp_path / 'selected', interval=3600)
    original = module.DemoSupervisor
    class OnePoll(original):
        def run(self, *, seconds=None):
            report = self.report
            def done(raw):
                report(raw)
                self.stop()
            self.report = done
            super().run(seconds=seconds)
    monkeypatch.setattr(module, 'DemoSupervisor', OnePoll)
    args = SimpleNamespace(execute=execute, interval='1h', max_backfill=120,
        poll_seconds=.001, seconds=60, strategy=str(tmp_path / 'selected'))
    counts = dict(GET=0, POST=0, DELETE=0)
    with closing(DemoRuntime(root)) as runtime:
        module.run_worker(args, root, store, runtime, counts)
    state = root / ('strategy-' + ('execute' if execute else 'dry-run') + '.sqlite3')
    with closing(module.StrategyControl(state)) as control:
        snapshot = control.snapshot()
    assert snapshot['configuration']['strategy']['spec']['strategy_id'] == 'close_momentum'
    assert snapshot['observed']['strategy']['last_signal']['strategy_id'] == 'close_momentum'
    assert counts['POST'] == int(execute)
