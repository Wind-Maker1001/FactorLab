from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import runpy

import pytest

from test_binance_preflight import demo_account_http
from factorlab.integrations.binance_futures_trading import demo_preflight
from factorlab.integrations.binance_futures_trading.protocol import Credentials, Environment

preparation = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/prepare_bounded_demo_quote.py'))
proposal = preparation['proposal']
verify_proposal = preparation['verify_proposal']


def observation():
    values = {
        'account_id': 'DEMO:FactorLab-Demo:' + 'a'*64,
        'PRICE_FILTER': dict(tickSize=D('.01'), minPrice=D('.01'), maxPrice=D('20000')),
        'LOT_SIZE': dict(stepSize=D('.01'), minQty=D('.01'), maxQty=D('6000')),
        'MIN_NOTIONAL': dict(notional=D('5')),
        'book': dict(bidPrice=D('169.51'), askPrice=D('169.56')),
        'mark': dict(markPrice=D('169.51')),
        'PERCENT_PRICE': dict(multiplierDown=D('.97'), multiplierUp=D('1.03')),
        'usdt': dict(availableBalance=D('5000')),
        'order_identity': dict(state='ABSENT'),
    }
    return dict(observed_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
                checks={key: dict(value=value, eligible=True) for key, value in values.items()})


def test_proposal_covers_entire_approved_price_interval_and_fee_reserve():
    result = proposal(observation(), 'm04-0123456789abcdef-01')
    policy = result['policy']
    assert result['status'] == 'PENDING_EXPLICIT_AUTHORIZATION'
    assert result['write_authorization'] is False
    assert result['observed_candidate_limit'] == D('168.66')
    assert policy['quantity'] == policy['max_quantity'] == D('.03')
    assert policy['quantity'] * policy['min_limit_price'] >= D('5')
    assert policy['max_notional'] >= policy['quantity'] * max(policy['max_limit_price'], policy['max_ask_price'])
    assert policy['max_margin'] >= policy['max_notional'] * D('1.001')
    assert policy['max_orders'] == policy['max_open_orders'] == 1
    assert policy['client_id_prefix'] == 'm04-0123456789abcdef'
    assert result['total_reserve'] == result['conservative_notional'] + result['fee_reserve']


@pytest.mark.parametrize('name', ['account_id', 'order_identity', 'book', 'mark'])
def test_unknown_or_failed_required_evidence_blocks_candidate(name):
    observed = observation()
    observed['checks'][name]['eligible'] = None
    result = proposal(observed, 'm04-test-01')
    assert result == {'status': 'P7_M04_BLOCKED', 'blockers': [name]}


def test_preparation_reviews_actual_fixed_quantity_and_controller_boundary(demo_account_http):
    payloads, _, _ = demo_account_http
    payloads['/fapi/v1/symbolConfig'][0]['leverage'] = 1
    def prepare(observation, facts, rules):
        observation['checks']['order_identity'] = dict(
            value={'clientOrderId': 'm04-test-01', 'state': 'ABSENT'}, eligible=True)
        return verify_proposal(observation, facts, rules)

    result = demo_preflight.inspect_demo(Credentials('k', 's', Environment.DEMO), prepare_policy=prepare)
    candidate = result['bounded_preparation']
    assert candidate['status'] == 'P7_M05_PENDING_MANUAL_EVIDENCE', candidate
    from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
    from factorlab.integrations.binance_futures_trading.bounded_quote import BoundedQuotePolicy, canonical_json
    plan = DemoExecutionPlan.from_json(canonical_json(candidate['plan']))
    policy = BoundedQuotePolicy.from_json(canonical_json(candidate['policy']))
    assert plan.quote_policy == policy
    assert plan.digest == candidate['plan_digest']
    assert policy.digest == candidate['policy_digest']
    assert candidate['approval_object_complete'] is True
    assert candidate['admission_reviews'] == dict(opening_plan={'LIMIT': ()},
        actual_fixed_quantity_rules='APPROVED', controller_observed='APPROVED', controller_upper_boundary='APPROVED')
    assert result['network_writes'] == 0


def test_proposal_rejects_insufficient_collateral():
    observed = observation()
    observed['checks']['usdt']['value']['availableBalance'] = D('1')
    assert proposal(observed, 'm04-test-01')['blockers'] == ['INSUFFICIENT_COLLATERAL']
