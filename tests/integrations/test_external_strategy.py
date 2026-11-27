"""External package examples and point-in-time input wiring without a network."""
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import shutil
from types import SimpleNamespace

import pandas as pd
import pytest

from examples.external_strategy.run import EXAMPLES, example_manifest, event_for
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.data.hub.hub import DataHub
from factorlab.integrations.alpaca.market_data.inputs import Instrument, normalize_bars
from factorlab.integrations.alpaca.scheduling import schedule_window
from factorlab.integrations.alpaca.strategy_assets import StrategyAssets
from factorlab.integrations.alpaca.strategy_data import StrategyData
from factorlab.integrations.binance_futures_trading.demo_instance_host import DemoInstanceHost
from factorlab.integrations.binance_futures_trading.demo_market_feed import SOURCE
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.strategies.decision_runtime import InstanceDecisionRuntime
from factorlab.strategies.deployment import DeploymentController, DeploymentStore, OfflineAdapter
from factorlab.strategies.events import StrategyInputUnavailable, validate_event
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.model import ModelNotEffective
from factorlab.strategies.package import build_package, install_package, manifest_spec
from factorlab.strategies.protocol import DecisionV2
from factorlab.strategies.reference_data import DataHubReferences
from factorlab.trading.portfolio_execution import PortfolioStore
from factorlab.trading.strategy import ClosedBar
from test_alpaca_instance_runtime import Account
from test_alpaca_scheduling import at, calendar
from test_binance_demo_execution import setup as binance_setup, NOW as BINANCE_NOW


UTC = timezone.utc
JAN6 = datetime(2026, 1, 6, 15, 30, tzinfo=UTC)
JAN13 = JAN6 + timedelta(days=7)


def install(root, example, **options):
    manifest = example_manifest(example, commit='a' * 40, **options)
    version = manifest['strategy_version']
    archive = root / f'{example}-{version}.zip'
    build_package(EXAMPLES, manifest, archive)
    return install_package(archive, root / 'releases')


@pytest.mark.parametrize('example,expected', [
    ('a', {'SPY': Decimal('.45'), 'IWM': Decimal('.45')}),
    ('b', {'SPY': Decimal('.9')}),
    ('c', {'NVDAUSDT': Decimal('.5')}),
])
def test_examples_build_install_and_run_in_isolated_worker(tmp_path, example, expected):
    release, _ = install(tmp_path, example)
    strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
    decision = strategy.decide(event_for(strategy.spec, JAN6), {})
    assert decision.targets == expected
    assert decision.state == {'events': 1}


def test_reference_source_basis_causality_and_order_authority(tmp_path):
    release, _ = install(tmp_path, 'b')
    strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
    spec = strategy.spec
    event = event_for(spec, JAN6)
    history = event['references']['IWM']['history']['closed_bar']
    with pytest.raises(StrategyInputUnavailable, match='reference symbols'):
        validate_event(dict(event, references={}), spec)
    with pytest.raises(ValueError, match='invalid target symbols'):
        DecisionV2('target_weights', targets={'IWM': Decimal(1)}).validate(spec)
    original = history[0].copy()
    history[0]['source'] = 'UNDECLARED'
    with pytest.raises(ValueError, match='reference source mismatch'):
        validate_event(event, spec)
    history[0].update(original)
    history[0]['payload'] = dict(original['payload'], price_basis='adjusted')
    with pytest.raises(ValueError, match='reference bar provenance'):
        validate_event(event, spec)
    history[0].update(original, observed_at=JAN6 + timedelta(seconds=1))
    with pytest.raises(ValueError, match='noncausal'):
        validate_event(event, spec)


def test_model_feature_shape_is_rejected_before_installation(tmp_path):
    manifest = example_manifest('c', commit='a' * 40)
    for name in manifest['files']:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLES / name, destination)
    (tmp_path / manifest['model']['file']).write_text(
        '{"schema":"FACTORLAB_LINEAR_MODEL_V1","features":["return_1"],"coefficients":[],"intercept":0}',
        encoding='utf-8')
    with pytest.raises(ValueError, match='matching coefficients'):
        build_package(tmp_path, manifest, tmp_path / 'incompatible.zip')


class OfflineExecution:
    def qualify(self, spec, binding, allocation):
        spec.require_binding(binding)

    def execute(self, receipt):
        pass


def test_model_switch_uses_versioned_receipts_and_rejects_backfill(tmp_path):
    first_release, first = install(tmp_path, 'c', model_version='1')
    second_release, second = install(tmp_path, 'c', model_version='2')
    versions = {
        first['package_id']: LoadedStrategy.from_strategy_package(first_release, trusted=True),
        second['package_id']: LoadedStrategy.from_strategy_package(second_release, trusted=True),
    }
    original = versions[first['package_id']]
    old_event = event_for(original.spec, JAN6)
    execution = OfflineExecution()
    database = tmp_path / 'deployment.db'
    with closing(DeploymentStore(database)) as registry:
        registry.register_instance(old_event['binding'])
        with closing(registry.for_instance('offline-example')) as view:
            controller = DeploymentController(view, OfflineAdapter(), load_strategy=versions.__getitem__)
            for i, manifest in enumerate((first, second)):
                view.request(manifest, dict(schema='FACTORLAB_CHAMPION_PROMOTION_V2', decision='PROMOTE',
                    qualified=True, strictly_better=True, binding=old_event['binding'],
                    expected_active=None if i == 0 else first['package_id'],
                    package_id=manifest['package_id'], selection_id=f'fixture-{i}', evidence=manifest['evidence']))
                assert controller.step()['active'] == manifest['package_id']
                strategy = versions[manifest['package_id']]
                instance = InstanceDecisionRuntime(strategy, view, execution, allocation={})
                if i == 0:
                    old_receipt = instance.decide(old_event)
                    assert old_receipt['decision']['targets'] == {'NVDAUSDT': Decimal('.5')}
                else:
                    with pytest.raises(ModelNotEffective):
                        instance.decide(old_event)
                    with pytest.raises(StrategyInputUnavailable, match='advance'):
                        instance.decide(dict(event_for(strategy.spec, JAN13), event_id=old_event['event_id']))
                    assert view.pending() is None
                    new_receipt = instance.decide(event_for(strategy.spec, JAN13))
                    assert new_receipt['decision']['targets'] == {'NVDAUSDT': Decimal(0)}
                    assert new_receipt['decision']['state'] == {'events': 2}
                    with pytest.raises(StrategyInputUnavailable, match='advance'):
                        instance.decide(event_for(strategy.spec, JAN13 - timedelta(minutes=1)))

    with closing(DeploymentStore(database, instance_id='offline-example')) as restarted:
        assert restarted.decision(first['package_id'], old_event['event_id'],
                                  old_receipt['input_identity']) == old_receipt
        assert old_receipt['model']['sha256'] != new_receipt['model']['sha256']
        restarted.rollback('offline fixture')
        controller = DeploymentController(restarted, OfflineAdapter(), load_strategy=versions.__getitem__)
        assert controller.step()['active'] == first['package_id']
        instance = InstanceDecisionRuntime(original, restarted, execution, allocation={})
        assert instance.decide(old_event) == old_receipt
        restored = instance.decide(event_for(original.spec, JAN13 + timedelta(days=1)))
        assert restored['decision']['state'] == {'events': 2}
        assert restored['model'] == old_receipt['model']


def test_alpaca_reference_history_requires_no_trading_qualification(tmp_path):
    now = at(24, 9, 31).astimezone(UTC)
    market_calendar = calendar()
    spec = manifest_spec(example_manifest('b', commit='a' * 40, account_id='paper',
                                         reference_source='ALPACA:iex'))
    binding = event_for(spec, now)['binding']

    class MarketAccount(Account):
        def read_asset(self, symbol):
            asset = super().read_asset(symbol)
            return dict(asset, tradable=False, fractionable=False) if symbol == 'IWM' else asset

    reads = []

    def history(instrument, start, end, *, timeframe, **kwargs):
        days = market_calendar.sessions(start, end).index
        reads.append((instrument.symbol, len(days), timeframe))
        return normalize_bars([dict(t=f'{day.date()}T05:00:00Z', o=10, h=12, l=9, c=10 + i, v=100)
            for i, day in enumerate(days)], instrument, calendar=market_calendar, timeframe=timeframe,
            received_at=now, feed='iex', source='ALPACA:iex')

    service = SimpleNamespace(calendar=market_calendar, history=history, client=SimpleNamespace(feed='iex'))
    with closing(PortfolioStore(tmp_path / 'portfolio.db')) as store:
        assets = StrategyAssets(MarketAccount(), store, clock=lambda: now)
        allocation = dict(capital=Decimal(100))
        assets.qualify(spec, binding, allocation)
        window = schedule_window(market_calendar, spec.schedule, now)
        event = StrategyData(assets, clock=lambda: now).event(service, spec, binding, allocation, window)
        validate_event(event, spec)
        assert set(event['symbols']) == {'SPY'}
        assert set(event['references']) == {'IWM'}
        assert set(reads) == {('SPY', 1, '1Day'), ('IWM', 2, '1Day')}
        assert store.event_payload('alpaca-asset:paper:IWM') is None


def test_datahub_reads_only_declared_source_and_requires_point_in_time():
    calls = []
    requirement = dict(source='FROZEN_FEATURES', history_limit=1)

    class Source:
        capabilities = frozenset({'strategy_prediction'})

        def read(self, kind, **query):
            calls.append((kind, query))
            frame = pd.DataFrame([dict(symbol='SPY.US', observed_at=JAN6, effective_at=JAN6,
                                       payload={'score': .25})])
            return QueryResult(frame, SourceMetadata('FROZEN_FEATURES', 'model-one:predictions'))

    source = Source()
    adapter = DataHubReferences(DataHub({'FROZEN_FEATURES': source, 'other': source}))
    assert adapter.supports('SPY.US', 'prediction', requirement)
    args = dict(start='2026-01-05', end='2026-01-06', as_of=JAN6)
    result = adapter.history('SPY.US', 'prediction', requirement, **args)
    assert result[0]['source_version'] == 'model-one:predictions'
    assert calls == [('strategy_prediction', dict(symbol='SPY.US', security_id=None,
        start='2026-01-05', end='2026-01-06', knowledge_as_of=JAN6.isoformat()))]
    with pytest.raises(StrategyInputUnavailable, match='knowledge cutoff'):
        adapter.history('SPY.US', 'prediction', requirement, **dict(args, as_of=JAN6 - timedelta(seconds=1)))


def test_alpaca_cuts_datahub_minutes_before_selecting_reference_history():
    now = at(24, 11, 15).astimezone(UTC)
    market_calendar = calendar()
    declaration = manifest_spec(example_manifest('b', commit='a' * 40, account_id='paper',
                                                reference_source='FROZEN_BARS'))
    spec = replace(declaration, schedule=dict(kind='interval', interval_seconds=3600), data=dict(
        declaration.data, references={'IWM': {'closed_bar': dict(source='FROZEN_BARS',
            price_basis='raw', interval_seconds=60, history_limit=2)}}))
    window = schedule_window(market_calendar, spec.schedule, now)
    binding = event_for(spec, now)['binding']

    class Source:
        capabilities = frozenset({'strategy_closed_bar'})

        def read(self, kind, **query):
            assert query['knowledge_as_of'] == now.isoformat()
            rows = [dict(symbol='IWM', observed_at=now,
                effective_at=window.signal_cutoff + timedelta(minutes=offset),
                payload=dict(price_basis='raw', interval_seconds=60, close=100 + offset))
                for offset in (-1, 0, 1, 2)]
            return QueryResult(pd.DataFrame(rows), SourceMetadata('FROZEN_BARS', 'vintage-one'))

    def history(instrument, start, end, *, timeframe):
        minutes = pd.date_range(at(24, 9, 30), periods=60, freq='min')
        rows = [dict(t=stamp.isoformat(), o=10, h=10, l=10, c=10, v=100) for stamp in minutes]
        return normalize_bars(rows, instrument, calendar=market_calendar, timeframe=timeframe,
                              received_at=now, feed='iex', source='ALPACA:iex')

    service = SimpleNamespace(calendar=market_calendar, history=history)
    assets = SimpleNamespace(instruments=lambda symbols: (Instrument('spy-id', symbols[0], now),))
    provider = DataHubReferences(DataHub({'FROZEN_BARS': Source()}))
    event = StrategyData(assets, clock=lambda: now, reference_provider=provider).event(
        service, spec, binding, {}, window)
    validate_event(event, spec)
    observations = event['references']['IWM']['history']['closed_bar']
    assert [entry['effective_at'] for entry in observations] == [
        window.signal_cutoff - timedelta(minutes=1), window.signal_cutoff]


@pytest.mark.parametrize('reference_first', [False, True])
def test_binance_reference_arrivals_complete_one_trade_event(tmp_path, monkeypatch, reference_first):
    declaration = manifest_spec(example_manifest('c', commit='a' * 40))
    spec = replace(declaration, data=dict(declaration.data, references={
        'ETHUSDT': {'closed_bar': dict(source=SOURCE, history_limit=2, price_basis='raw',
                                      interval_seconds=60)}}))
    adapter, gateway, controller, _ = binance_setup()
    binding = dict(event_for(spec, JAN6)['binding'], account_id=adapter.account_id)
    spec = replace(spec, account_id=adapter.account_id)
    now = BINANCE_NOW
    with ExitStack() as stack:
        stack.callback(adapter.store.close)
        stack.callback(controller.store.close)
        capital = stack.enter_context(closing(PortfolioStore(tmp_path / 'portfolio.sqlite3')))
        capital.db.execute('INSERT INTO portfolio_allocations VALUES(?,?,?,?,?)',
            (adapter.account_id, binding['instance_id'], binding['allocation_id'], '7', '7'))
        registry = stack.enter_context(closing(DeploymentStore(tmp_path / 'deployment.db')))
        runtime = stack.enter_context(closing(DemoRuntime(tmp_path)))
        session = SimpleNamespace(adapter=adapter, gateway=gateway, controller=controller,
            _now=lambda: now, instance_owner=None, status=lambda: {})
        host = DemoInstanceHost(session, runtime, registry, tmp_path / 'releases', tmp_path, stack,
                                SimpleNamespace(desired=lambda: 'RUNNING'))
        host.owner.instances['offline-example'] = (SimpleNamespace(package_id='package',
            strategy=SimpleNamespace(spec=spec, binding={}),
            store=SimpleNamespace(binding=lambda: binding)), None)
        observations = []

        def deliver(owner, event):
            validate_event(event, spec)
            observations.append(event)
            return owner

        monkeypatch.setattr(host, 'deliver', deliver)

        def bar(symbol, closed):
            price = Decimal(10)
            return ClosedBar(symbol, closed - timedelta(minutes=1), closed, price,
                price, price, price, Decimal(1), 60, SOURCE)

        first, second = ('ETHUSDT', 'NVDAUSDT') if reference_first else ('NVDAUSDT', 'ETHUSDT')
        previous = now - timedelta(minutes=1)
        host.closed_bar(bar(first, previous), historical=True)
        host.closed_bar(bar(second, previous), historical=True)
        assert host.closed_bar(bar(first, now)) == {}
        assert host.closed_bar(bar(second, now)) == {'offline-example': 'offline-example'}
        assert len(observations) == 1
        assert set(observations[0]['symbols']) == {'NVDAUSDT'}
        assert set(observations[0]['references']) == {'ETHUSDT'}
