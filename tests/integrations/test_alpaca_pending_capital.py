"""Protect Alpaca capital while a V2 decision awaits its portfolio journal entry."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.protocol import DecisionV2, decision_payload
from factorlab.trading.capital_cli import command_result, enqueue, process_command
from factorlab.trading.portfolio_execution import plan_identity
from test_alpaca_instance_runtime import Account
from test_alpaca_instance_switch import package, request
from test_alpaca_portfolio import NOW
from test_alpaca_scheduling import calendar


class OfficialAccount(Account):
    def read_account(self):
        return replace(super().read_account(), official_equity=D(100))


def test_restarted_alpaca_owner_protects_frozen_decision_before_plan_and_ack(tmp_path):
    manifest = package(tmp_path, '1', symbols=('SPY',))
    successor = package(tmp_path, '2', symbols=('SPY',))
    package_id = manifest['package_id']
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
    service = SimpleNamespace(calendar=calendar(), client=SimpleNamespace(feed='iex'))
    event_id = 'committed-before-plan'
    session = '2026-11-24'
    identity = plan_identity('paper', 'donor', session, package_id + ':' + event_id)
    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        assert host.controllers['donor'].step()['active'] == package_id
        view = host.views['donor']
        snapshot = view.snapshot()
        event = dict(binding=donor, allocation={'positions': {}},
                     context={'execution_session': session})
        decision = DecisionV2('quantity_deltas', targets={'SPY': D(1)}, state={'events': 1})
        # The process stops after committing the receipt and before creating or acknowledging its plan.
        view.commit_decision(package_id, event_id, 'frozen-event', decision_payload(decision),
            event=event, expected_checkpoint=snapshot['checkpoint'],
            expected_watermark=snapshot['watermark'])
        assert host.store.plan(identity) is None

    with closing(StrategyInstances(tmp_path, account, configuration, clock=lambda: NOW)) as host:
        transfer = dict(action='change', event_id='early-transfer', source='donor',
                        destination='recipient', amount=D(10))
        enqueue(host.store, 'paper', 'transfer-command', transfer)
        process_command(host.capital, 'paper')
        command = command_result(host.store, 'paper', 'transfer-command')
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
        released = host.capital.change(account_id='paper', event_id='after-replay',
            source='donor', destination=None, amount=D(10))
        assert released.status == 'COMPLETE' and released.applied == D(10)
        assert host.store.cash('paper', 'donor') == D(30)
        assert host.store.cash('paper', 'recipient') == D(40)
        assert account.cash == D(100)
        assert account.posts == []

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
