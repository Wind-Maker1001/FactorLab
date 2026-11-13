from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from test_binance_bounded_quote import setup, candidate, wired, network_clock
from factorlab.integrations.binance_futures_trading.bounded_quote import (
    BoundedQuotePolicy, rule_identity, configuration_identity, content_digest, execution_code_identity,
)
from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.integrations.binance_futures_trading.quantitative import observe_quantitative


def approved(wired):
    a, g, c, p, o = setup(wired)
    p = replace(p, version=2, rules_identity=rule_identity(g.rules),
        configuration_identity=configuration_identity(a.facts, p.symbol),
        account_observation_identity=content_digest(a.facts), quote_observed_at=o.observed_at,
        execution_code_identity=execution_code_identity(), approval_record_id='unapproved:test', tick_size=D('.01'))
    authorization = a.client.transport.authorization
    authorization._plan = replace(authorization.plan, quote_policy=p)
    return a, g, p, o


def test_complete_roundtrip(wired):
    a, g, p, o = approved(wired)
    plan = a.client.transport.authorization.plan
    assert BoundedQuotePolicy.from_json(p.to_json()) == p
    assert DemoExecutionPlan.from_json(plan.to_json()) == plan
    assert DemoExecutionPlan.from_json(plan.to_json()).digest == plan.digest
    assert candidate(g, a, o)[0].client_order_id == p.client_order_id


@pytest.mark.parametrize('field,value', [
    ('quantity', D('1.01')), ('max_fee', D('2')), ('max_funds', D('107')),
    ('max_notional', D('106')), ('offset_bps', D('51')), ('max_ask', D('103')),
    ('min_price', D('97')), ('max_price', D('102')), ('tick_size', D('.02')),
    ('rules_identity', 'b'*64), ('configuration_identity', 'b'*64),
    ('execution_code_identity', 'b'*64), ('account_observation_identity', 'b'*64),
    ('approval_record_id', 'unapproved:other'), ('client_order_id', 'other'),
])
def test_authority_field_changes_hash(wired, field, value):
    a, g, p, o = approved(wired)
    assert replace(p, **{field:value}).digest != p.digest
    assert replace(p, expires_at=p.expires_at+timedelta(seconds=1)).digest != p.digest


def test_changed_approved_rules_rejected_even_with_fresh_capture(wired):
    a, g, p, o = approved(wired)
    from factorlab.integrations.binance_futures_trading.bounded_quote import canonical_json
    rules = replace(g.rules, source_version='changed')
    with pytest.raises(SafetyBlocked, match='APPROVED_RULES'):
        from factorlab.integrations.binance_futures_trading.bounded_quote import bounded_quote
        bounded_quote(a.client.transport.authorization.plan, replace(o, rules_json=canonical_json(rules)),
            facts=a.facts, rules=rules, now=o.observed_at)


def test_missing_indicators_are_not_lock_clearance():
    missing = observe_quantitative({'indicators':{}, 'updateTime':1})
    assert missing.report('NVDAUSDT')['scopes']['ACCOUNT'] == {'status':'NOT_REPORTED'}
    assert not missing.error
    locked = observe_quantitative({'indicators':{'ACCOUNT':[{'indicator':'UFR','isLocked':True}]}})
    assert locked.applicable_locks('NVDAUSDT')
    assert observe_quantitative({'indicators':None}).error == 'MALFORMED_RESPONSE'


@pytest.mark.parametrize('field,value', [('max_limit_posts',2),('max_known_order_deletes',2),
    ('automatic_resubmit',True),('automatic_market',True),('automatic_close',True)])
def test_write_contract_cannot_expand(wired, field, value):
    a, g, p, o = approved(wired)
    with pytest.raises(ValueError):
        replace(p, **{field:value})
