from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json

import pytest

from factorlab.experiments.grid_f11_f08_offline import default_demo_rules, _demo_grid_config
from factorlab.experiments.grid_funding_adaptive import FundingSensitiveGrid, FundingPolicy, VolatilityAdaptiveGrid, VolatilityPolicy
from factorlab.strategies.adaptive_grid_v2 import AdaptiveGridV2
from factorlab.strategies.protocol import V2
from factorlab.trading import Market, OrderStatus
from factorlab.trading.storage import dumps, decode
from test_strategy_protocol_v2 import spec

NOW = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)


def fixture(kind):
    rules = default_demo_rules()
    config = _demo_grid_config(D('132.89'), rules, max_cash_committed=D(7), fee_rate=D('.0003'), slippage=D(0))
    arguments = dict(deployment_id='source', account_id='account', market=Market.BINANCE_USDM,
        instrument_id='NVDAUSDT', initial_equity=D(7), fee_rate=D('.0003'), continuous_capacity=True)
    if kind == 'funding':
        grid = FundingSensitiveGrid(config, rules, **arguments, funding_policy=FundingPolicy(),
            funding_events=[], use_f11_limit_exit=True)
    else:
        grid = VolatilityAdaptiveGrid(config, rules, **arguments, vol_policy=VolatilityPolicy(), warmup_closes=[132.89]*100)
    declaration = spec('orders', market=Market.BINANCE_USDM.value, account_id='account', symbols=['NVDAUSDT'],
        strategy_id=kind, capital=dict(currency='USDT', requested=D(7)),
        data=dict(data_types=['closed_bar', 'order_event', 'funding_settlement'], history_limit=100,
                  historical_observations=False), schedule=dict(kind='interval', interval_seconds=3600),
        execution=dict(order_types=['LIMIT'], time_in_force=['GTC'], shorting=False,
                       fractional_quantities=True, reduce_only=True))
    strategy = AdaptiveGridV2(grid.deployment(), asdict(declaration))
    account = dict(inventory=D(0), available=D(7), equity=D(7), committed=D(0), turnover=D(0),
        account_id='account', instrument_id='NVDAUSDT', market=Market.BINANCE_USDM, reconciled=True)
    bar = dict(symbol='NVDAUSDT', opened_at=NOW-timedelta(hours=1), closed_at=NOW, open=D('132.89'),
        high=D('132.89'), low=D('132.89'), close=D('132.89'), volume=D(1), interval_seconds=3600,
        source='BINANCE_USDM_PUBLIC_REST', complete=True)
    funding = [dict(observed_at=NOW, effective_at=NOW-timedelta(hours=hours), source='OFFICIAL_DEMO',
        payload=dict(event_id='settlement-'+str(hours), funding_rate=D(0), mark_price=D('132.89'))) for hours in (25, 1)]
    event = dict(protocol=V2, binding=dict(market=Market.BINANCE_USDM.value, account_id='account',
        instance_id='instance', allocation_id='seven', strategy_id=kind), event_id='bar-one', event_time=NOW,
        data_type='closed_bar', symbols={'NVDAUSDT': dict(watermark='one',
            observations={'closed_bar': dict(observed_at=NOW, effective_at=NOW, source=bar['source'], payload=bar)},
            history={'funding_settlement': funding})}, account={}, allocation=dict(capital=D(7)),
        context=dict(package_id='package', grid_accounts={'NVDAUSDT': account},
            grid_rules={'NVDAUSDT': {key: value for key, value in asdict(rules).items() if key != 'quantity_unit'}},
            execution=dict(native_to_client={})))
    return strategy, event


def roundtrip(value):
    return decode(json.loads(dumps(value)))


@pytest.mark.parametrize('kind', ['funding', 'volatility'])
def test_native_adaptive_grid_proposes_confirms_reverses_and_restores(kind):
    strategy, event = fixture(kind)
    first = strategy.decide(event, {}).validate(strategy.spec)
    assert first.orders
    entry = first.orders[0]
    assert not entry.reduce_only
    event = roundtrip(event)
    event['context']['execution']['native_to_client'][entry.client_order_id] = 'original-account-id'
    # A subsequent completed bar does not offer the already accepted entry a second time.
    event['event_id'] = 'bar-two'
    event['event_time'] += timedelta(hours=1)
    bar = event['symbols']['NVDAUSDT']['observations']['closed_bar']['payload']
    bar['opened_at'] += timedelta(hours=1)
    bar['closed_at'] += timedelta(hours=1)
    second = strategy.decide(event, roundtrip(first.state))
    assert all(order.client_order_id != entry.client_order_id for order in second.orders)
    price, quantity = entry.limit_price, entry.quantity
    filled = price * quantity
    account = event['context']['grid_accounts']['NVDAUSDT']
    account.update(inventory=quantity, available=D(7)-filled, committed=filled, turnover=filled)
    event['data_type'] = 'order_event'
    event['event_id'] = 'original-confirmation'
    event['symbols']['NVDAUSDT']['observations']['order_event'] = dict(observed_at=event['event_time'],
        effective_at=event['event_time'], source='OFFICIAL_DEMO', payload=dict(client_order_id='original-account-id',
        exchange_order_id='official-order', status=OrderStatus.FILLED, cumulative_filled_quantity=quantity,
        fill_price=price, event_time=event['event_time'], source='OFFICIAL_DEMO', event_id='official-fill',
        cumulative_fee=D(0), cumulative_filled_notional=filled))
    restored = AdaptiveGridV2(strategy.deployment, asdict(strategy.spec))
    submitted = roundtrip(event)
    submitted['event_id'] = 'original-submission'
    submitted['context']['grid_accounts']['NVDAUSDT'].update(inventory=D(0), available=D(7)-filled,
        committed=filled, turnover=D(0))
    submitted['symbols']['NVDAUSDT']['observations']['order_event']['payload'].update(
        status=OrderStatus.SUBMITTED, cumulative_filled_quantity=D(0), fill_price=None,
        event_id='official-submitted', cumulative_filled_notional=D(0))
    acknowledged = restored.decide(submitted, roundtrip(second.state))
    confirmed = restored.decide(roundtrip(event), roundtrip(acknowledged.state)).validate(strategy.spec)
    assert confirmed.orders and all(order.reduce_only for order in confirmed.orders)
    assert sum(order.quantity for order in confirmed.orders) == quantity
    assert restored.decide(roundtrip(event), roundtrip(acknowledged.state)) == confirmed
    checkpoint = decode(json.loads(confirmed.state['grid']))
    assert checkpoint['account']['inventory'] == quantity
    if kind == 'funding':
        assert checkpoint['subclass_state']['entry_time_utc'] is not None
    else:
        assert len(checkpoint['subclass_state']['close_history']) > 24


@pytest.mark.parametrize('kind', ['funding', 'volatility'])
def test_native_adaptive_grid_runs_in_isolated_package(tmp_path, kind):
    from factorlab.strategies.package import build_package, install_package
    from factorlab.strategies.loader import LoadedStrategy
    strategy, event = fixture(kind)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'strategy.py').write_text(
        'import json\nfrom factorlab.trading.storage import decode\n'
        'from factorlab.strategies.adaptive_grid_v2 import AdaptiveGridV2\n'
        'class Strategy(AdaptiveGridV2):\n'
        '    def __init__(self):\n'
        '        super().__init__(decode(json.loads(' + repr(dumps(strategy.deployment)) + ')), '
        'decode(json.loads(' + repr(dumps(asdict(strategy.spec))) + ')))\n', encoding='utf-8')
    declaration = asdict(strategy.spec)
    requirements = {key: declaration[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital'] = dict(requirements['capital'], requested='7')
    manifest = dict(schema_version=2, interface=V2, strategy_id=strategy.spec.strategy_id,
        strategy_version='2', market=strategy.spec.market, account_id=strategy.spec.account_id,
        symbols=strategy.spec.symbols, execution_mode='orders', requirements=requirements,
        code_commit='a'*40, entrypoint='strategy:Strategy', files={'strategy.py': ''}, configuration={},
        dependencies=[], evidence={'fixture': 'native-adaptive-grid'}, migration='wait_flat')
    archive = tmp_path / 'strategy.zip'
    build_package(source, manifest, archive)
    release, _ = install_package(archive, tmp_path / 'releases')
    loaded = LoadedStrategy.from_strategy_package(release, trusted=True)
    expected = strategy.decide(event, {}).validate(strategy.spec)
    assert dumps(asdict(loaded.decide(event, {}))) == dumps(asdict(expected))
    assert dumps(asdict(loaded.decide(event, roundtrip(expected.state)))) == dumps(
        asdict(strategy.decide(event, roundtrip(expected.state))))


def test_funding_grid_waits_for_complete_signal_window():
    strategy, event = fixture('funding')
    event['symbols']['NVDAUSDT']['history']['funding_settlement'] = []
    decision = strategy.decide(event, {})
    assert not decision.orders
    assert decision.state['funding'] == {}


def test_funding_grid_accumulates_signal_evidence_across_events():
    strategy, event = fixture('funding')
    history = event['symbols']['NVDAUSDT']['history']['funding_settlement']
    event['symbols']['NVDAUSDT']['history']['funding_settlement'] = history[:1]
    waiting = strategy.decide(event, {})
    assert not waiting.orders
    assert set(waiting.state['funding']) == {'settlement-25'}
    event = roundtrip(event)
    event['event_id'] = 'bar-with-new-funding'
    event['symbols']['NVDAUSDT']['history']['funding_settlement'] = history[1:]
    ready = strategy.decide(event, roundtrip(waiting.state))
    assert ready.orders
