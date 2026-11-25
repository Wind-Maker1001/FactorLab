from dataclasses import asdict, replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from examples.alpaca_balanced.strategy import BalancedPortfolio
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.package import build_package, install_package
from test_alpaca_instance_runtime import Account
from test_alpaca_portfolio import NOW
from test_alpaca_scheduling import calendar


def package(tmp_path, version, symbols=('SPY', 'IWM')):
    source = tmp_path / ('source-' + version)
    source.mkdir()
    spec = replace(BalancedPortfolio('paper', capital='40').describe(), version=version, symbols=symbols,
                   decision_kind='quantity_deltas', data=dict(data_types=['order_event'],
                       history_limit=1, historical_observations=False))
    (source / 'strategy.py').write_text('''from decimal import Decimal
from factorlab.strategies.protocol import StrategySpecV2, DecisionV2
class Strategy:
    def describe(self):
        return StrategySpecV2(**SPEC)
    def decide(self, event, state):
        return DecisionV2('quantity_deltas', state={'events': state.get('events', 0) + 1})
'''.replace('SPEC', repr(asdict(spec))), encoding='utf-8')
    fields = asdict(spec)
    requirements = {key: fields[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital']['requested'] = '40'
    archive = tmp_path / (version + '.zip')
    manifest = build_package(source, dict(schema_version=2, interface='FACTORLAB_DECISION_V2',
        strategy_id=spec.strategy_id, strategy_version=version, market=spec.market, account_id='paper',
        symbols=spec.symbols, execution_mode=spec.decision_kind, requirements=requirements,
        code_commit='a' * 40, entrypoint='strategy:Strategy', files={'strategy.py': ''},
        configuration={}, dependencies=[], evidence={'version': version}, migration='wait_flat'), archive)
    install_package(archive, tmp_path / 'releases')
    return manifest


def request(view, manifest, selection):
    view.request(manifest, dict(schema='FACTORLAB_CHAMPION_PROMOTION_V2', binding=view.binding(),
        selection_id=selection, decision='PROMOTE', qualified=True, strictly_better=True,
        expected_active=view.snapshot()['active'], package_id=manifest['package_id'],
        evidence=manifest['evidence']))


def test_conflicting_capital_promotion_does_not_activate_candidate(tmp_path):
    old = package(tmp_path, '1', symbols=('SPY',))
    new = package(tmp_path, '2', symbols=('IWM',))
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper',
        strategy_id='alpaca-balanced', instance_id='one', allocation_id='one-usd')
    store = DeploymentStore(tmp_path / 'deployments.sqlite3')
    store.register_instance(binding)
    view = store.for_instance('one')
    request(view, old, 'initial')
    view.close()
    store.close()
    account = Account()
    account.http = SimpleNamespace(request=lambda *args, **kwargs: [])
    account.read_corporate_activities = lambda **kwargs: []
    configuration = dict(releases=str(tmp_path / 'releases'), trusted_python=True,
        capital_policy=dict(currency='USD', budget='100', fee_rate='0'),
        instances=[dict(binding=binding, funding=dict(capital='40', initial_cash='40'))])
    service = SimpleNamespace(calendar=calendar(), client=SimpleNamespace(feed='iex'))
    host = StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)
    host.tick(service, {}, NOW, account.read_account_details())
    with host.store.transaction():
        host.store.capital_policy.claim('paper', 'other', 'IWM')
    request(host.views['one'], new, 'conflicting')
    host.tick(service, {}, NOW, account.read_account_details())
    assert host.views['one'].snapshot()['active'] == old['package_id']
    assert host.views['one'].pending()['phase'] == 'ROLLBACK'
    host.close()
    host = StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)
    host.tick(service, {}, NOW, account.read_account_details())
    assert host.views['one'].snapshot()['active'] == old['package_id']
    assert host.views['one'].pending() is None
    assert not account.posts
    host.close()


@pytest.mark.parametrize('failed_activation', [False, True])
def test_hot_switch_and_restart_preserve_other_instances_checkpoint(tmp_path, failed_activation):
    old, new = package(tmp_path, '1'), package(tmp_path, '2')
    registrations = []
    store = DeploymentStore(tmp_path / 'deployments.sqlite3')
    for owner in ('one', 'two'):
        binding = dict(market='ALPACA_US_EQUITY', account_id='paper',
            strategy_id='alpaca-balanced', instance_id=owner, allocation_id=owner + '-usd')
        store.register_instance(binding)
        view = store.for_instance(owner)
        request(view, old, 'initial-' + owner)
        view.close()
        registrations.append(dict(binding=binding, funding={'capital': '40', 'initial_cash': '40'}))
    store.close()
    account = Account()
    account.http = SimpleNamespace(request=lambda *args, **kwargs: [])
    account.read_corporate_activities = lambda **kwargs: []
    service = SimpleNamespace(calendar=calendar(), client=SimpleNamespace(feed='iex'))
    configuration = dict(releases=str(tmp_path / 'releases'), trusted_python=True, instances=registrations)
    host = StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)
    host.tick(service, {}, NOW, account.read_account_details())
    assert host.views['one'].checkpoint() == host.views['two'].checkpoint() == {'events': 1}
    request(host.views['one'], new, 'switch-one')
    if failed_activation:
        def unavailable(version):
            raise OSError('activation interrupted')
        host.controllers['one'].adapter.activated = unavailable
        host.tick(service, {}, NOW, account.read_account_details())
        assert host.views['one'].pending() is None
        assert host.views['one'].activation_callback()['reason'] == 'OSError'
        assert host.views['one'].checkpoint() == {'events': 1}
        assert host.state['instances']['one']['state'] == 'SWITCHING'
        assert host.views['two'].checkpoint() == {'events': 1}
    host.close()
    host = StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)
    for _ in range(2):
        host.tick(service, {}, NOW, account.read_account_details())
    assert host.views['one'].snapshot()['active'] == new['package_id']
    assert host.views['one'].checkpoint() == {'events': 2}
    assert host.views['two'].snapshot()['active'] == old['package_id']
    assert host.views['two'].checkpoint() == {'events': 1}
    reports = host.report()
    assert reports['one']['cash'] == reports['two']['cash'] == D(40)
    assert reports['one']['binding']['allocation_id'] == 'one-usd'
    assert not account.posts
    host.close()
