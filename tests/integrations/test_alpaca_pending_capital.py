"""Protect accepted Alpaca decisions until their portfolio obligations are settled."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.adapter import OrderNotFound
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.protocol import DecisionV2, decision_payload
from factorlab.trading.capital_cli import command_result, enqueue, process_command
from factorlab.trading.contracts import Market, OrderIntent, OrderType, Side
from factorlab.trading.portfolio_execution import ExecutionQuote, InstrumentRules, plan_identity
from test_alpaca_instance_runtime import Account
from test_alpaca_instance_switch import package, request
from test_alpaca_portfolio import NOW
from test_alpaca_scheduling import calendar


SESSION = '2026-11-24'


class OfficialAccount(Account):
    def read_account(self):
        return replace(super().read_account(), official_equity=D(100))

    def read_order_details(self, client_order_id):
        if client_order_id not in self.details:
            raise OrderNotFound(404)
        return super().read_order_details(client_order_id)


def configured_owner(tmp_path, manifest):
    donor = dict(market='ALPACA_US_EQUITY', account_id='paper',
                 strategy_id='alpaca-balanced', instance_id='donor', allocation_id='donor-usd')
    recipient = dict(donor, instance_id='recipient', allocation_id='recipient-usd')
    with closing(DeploymentStore(tmp_path / 'deployments.sqlite3')) as deployments:
        deployments.register_instance(donor)
        with closing(deployments.for_instance('donor')) as view:
            request(view, manifest, 'first-promotion')
    configuration = dict(releases=str(tmp_path / 'releases'), trusted_python=True,
        capital_policy=dict(currency='USD', budget='100', fee_rate='0'),
        instances=[dict(binding=binding, funding=dict(capital='40', initial_cash='40'))
                   for binding in (donor, recipient)])
    account = OfficialAccount()
    account.http = SimpleNamespace(request=lambda *args, **kwargs: [])
    account.read_corporate_activities = lambda **kwargs: []
    return account, configuration


def freeze_decision(host, package_id, decision):
    assert host.controllers['donor'].step()['active'] == package_id
    view = host.views['donor']
    snapshot = view.snapshot()
    event = dict(binding=view.binding(), allocation={'positions': {}},
                 context={'execution_session': SESSION})
    event_id = 'committed-before-plan'
    view.commit_decision(package_id, event_id, 'frozen-event', decision_payload(decision),
        event=event, expected_checkpoint=snapshot['checkpoint'],
        expected_watermark=snapshot['watermark'])
    return plan_identity('paper', 'donor', SESSION, package_id + ':' + event_id)


def run_capital_command(host, command_id, **request):
    enqueue(host.store, 'paper', command_id, dict(event_id=command_id, **request))
    process_command(host.capital, 'paper')
    return command_result(host.store, 'paper', command_id)


def test_restarted_alpaca_owner_protects_frozen_decision_before_plan_and_ack(tmp_path):
    manifest = package(tmp_path, '1', symbols=('SPY',))
    successor = package(tmp_path, '2', symbols=('SPY',))
    package_id = manifest['package_id']
    account, configuration = configured_owner(tmp_path, manifest)
    service = SimpleNamespace(calendar=calendar(), client=SimpleNamespace(feed='iex'))

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        identity = freeze_decision(host, package_id, DecisionV2(
            'quantity_deltas', targets={'SPY': D(1)}, state={'events': 1}))
        assert host.store.plan(identity) is None

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        command = run_capital_command(host, 'early-transfer', action='change', source='donor',
                                      destination='recipient', amount=D(10))
        assert command['status'] == 'COMPLETE', command
        result = command['data']
        assert result['status'] == 'INSUFFICIENT_CAPITAL'
        assert result['executable'] == result['applied'] == 0
        with pytest.raises(ValueError, match='decision'):
            host.capital.release(account_id='paper', event_id='early-release', strategy_id='donor')
        before = {row.strategy_id: row for row in host.capital.snapshot('paper').strategies}
        assert before['donor'].withdrawable == 0
        assert before['donor'].cash == before['recipient'].cash == D(40)
        assert before['recipient'].withdrawable == D(40)

        host.instances['donor'].runtime.recover()
        assert host.views['donor'].outstanding(package_id) == []
        assert host.store.plan(identity)['request']['quantity_deltas'] == {'SPY': D(1)}
        assert host.store.orders(identity) == []
        host.runner.executor.abandon(identity)
        released = host.capital.change(account_id='paper', event_id='after-abandon',
            source='donor', destination=None, amount=D(10))
        assert released.status == 'COMPLETE' and released.applied == D(10)
        assert host.store.cash('paper', 'donor') == D(30)
        assert host.store.cash('paper', 'recipient') == D(40)

        request(host.views['donor'], successor, 'resize-then-switch')

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        host.tick(service, {}, NOW, account.read_account_details())
        assert host.views['donor'].snapshot()['active'] == successor['package_id']
        instance = host.instances['donor']
        assert instance.runtime.package_id == successor['package_id']
        allocation = instance.allocation(account.read_account())
        assert (allocation['allocation_id'], allocation['capital'], allocation['cash']) == (
            'donor-usd', D(30), D(30))
        assert host.store.plan(identity)['request']['quantity_deltas'] == {'SPY': D(1)}
        assert host.store.cash('paper', 'recipient') == D(40)
        assert account.cash == D(100)
        assert not account.posts


@pytest.mark.parametrize('kind', ['target_weights', 'target_quantities', 'quantity_deltas'])
def test_acknowledged_alpaca_plan_protects_cash_after_restart(tmp_path, kind):
    manifest = package(tmp_path, '1', symbols=('SPY',), decision_kind=kind)
    package_id = manifest['package_id']
    account, configuration = configured_owner(tmp_path, manifest)

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        target = D('.25') if kind == 'target_weights' else D(1)
        identity = freeze_decision(host, package_id, DecisionV2(kind, targets={'SPY': target}))
        host.instances['donor'].runtime.recover()
        assert host.views['donor'].outstanding(package_id) == []
        assert host.store.plan(identity)['phase'] == 'REDUCE'
        assert host.store.orders(identity) == []

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        assert host.views['donor'].outstanding(package_id) == []
        for command_id, destination in [('withdraw-all', None), ('transfer-all', 'recipient')]:
            command = run_capital_command(host, command_id, action='change', source='donor',
                                          destination=destination, amount=D(40))
            assert command['status'] == 'COMPLETE', command
            assert command['data']['status'] == 'INSUFFICIENT_CAPITAL'
            assert command['data']['applied'] == command['data']['executable'] == 0
        exit_command = run_capital_command(host, 'release-donor', action='release', strategy_id='donor')
        assert exit_command['status'] == 'REJECTED'
        assert 'decision' in exit_command['reason']
        sleeves = {row.strategy_id: row for row in host.capital.snapshot('paper').strategies}
        assert sleeves['donor'].cash == sleeves['recipient'].cash == D(40)
        assert sleeves['donor'].withdrawable == 0
        assert sleeves['recipient'].withdrawable == D(40)
        unaffected = run_capital_command(host, 'unaffected-owner', action='change', source='recipient',
                                         destination=None, amount=D(5))
        assert unaffected['data']['status'] == 'COMPLETE'
        assert unaffected['data']['applied'] == D(5)

        if kind == 'target_weights':
            quotes = {'SPY': ExecutionQuote('SPY', D(10), D(10), NOW, NOW, SESSION, 'offline')}
            rules = {'SPY': InstrumentRules(D(1), D(1))}
            host.runner.run_once(identity, quotes=quotes, rules=rules)
            assert host.runner.run_once(identity, quotes=quotes, rules=rules).phase == 'COMPLETE'
            assert [order.quantity for order in account.posts] == [D(1)]
            withdrawable = D(30)
        else:
            assert host.runner.run_once(identity, quotes={}, rules={}).phase == 'BLOCKED'
            assert host.store.orders(identity) == []
            assert host.pending_commitments('paper') == {'donor': None}
            host.runner.executor.abandon(identity)
            blocked = host.store.plan(identity)
            assert blocked['phase'] == 'COMPLETE' and blocked['abandoned']
            assert account.posts == []
            withdrawable = D(40)

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        released = run_capital_command(host, 'withdraw-settled', action='change', source='donor',
                                       destination=None, amount=withdrawable)
        assert released['data']['status'] == 'COMPLETE'
        assert released['data']['applied'] == withdrawable
        assert host.store.cash('paper', 'recipient') == D(35)




def test_direct_order_reservation_replaces_the_unacknowledged_commitment(tmp_path):
    manifest = package(tmp_path, '1', symbols=('SPY',), decision_kind='orders')
    package_id = manifest['package_id']
    account, configuration = configured_owner(tmp_path, manifest)
    intent = OrderIntent(Market.ALPACA_US_EQUITY, 'SPY', Side.BUY, OrderType.LIMIT,
                         D(1), D(10), 'direct-donor-buy', time_in_force='DAY')

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        identity = freeze_decision(host, package_id, DecisionV2('orders', orders=(intent,)))
        receipt, = host.views['donor'].outstanding(package_id)
        assert host.pending_commitments('paper') == {'donor': None}
        host.instances['donor'].runtime.adapter.execute(receipt)
        assert host.views['donor'].outstanding(package_id)
        assert len(host.store.orders(identity)) == 1

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        assert host.views['donor'].outstanding(package_id)
        donor = next(row for row in host.capital.snapshot('paper').strategies if row.strategy_id == 'donor')
        assert not donor.pending_decisions
        assert donor.reserved == D(10)
        assert donor.withdrawable == D(30)
        moved = run_capital_command(host, 'direct-excess-cash', action='change',
                                    source='donor', destination='recipient', amount=D(30))
        assert moved['data']['applied'] == D(30)
        assert host.store.cash('paper', 'donor', reserve=False) == D(10)
        assert host.store.cash('paper', 'donor') == 0
        assert host.store.allocated_capital('paper') == D(80)
        host.instances['donor'].runtime.recover()
        assert host.views['donor'].outstanding(package_id) == []
        assert not account.posts
