from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from test_binance_bounded_quote import setup, candidate, wired, network_clock
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


def test_cross_day_same_instant(wired):
    a, g, c, p, o = setup(wired)
    beijing = p.expires_at.astimezone(timezone(timedelta(hours=8)))
    assert beijing == p.expires_at
    assert beijing.astimezone(timezone.utc) == p.expires_at


@pytest.mark.parametrize('seconds,expired', [(-1, False), (0, True), (1, True)])
def test_policy_absolute_expiry(wired, seconds, expired):
    a, g, c, p, o = setup(wired)
    plan = a.client.transport.authorization.plan
    now = p.expires_at + timedelta(seconds=seconds)
    fresh = replace(o, observed_at=now, mark_at=now, book_at=now)
    from factorlab.integrations.binance_futures_trading.bounded_quote import bounded_quote
    if expired:
        with pytest.raises(SafetyBlocked, match='EXPIRED'):
            bounded_quote(plan, fresh, facts=a.facts, rules=g.rules, now=now)
    else:
        # Historical account staleness is separate from policy expiry.
        facts = replace(a.facts, observed_at=now)
        from factorlab.integrations.binance_futures_trading.bounded_quote import canonical_json
        bounded_quote(plan, replace(fresh, account_json=canonical_json(facts)), facts=facts, rules=g.rules, now=now)


@pytest.mark.parametrize('field', ['observed_at', 'mark_at', 'book_at'])
@pytest.mark.parametrize('age,stale', [(5, False), (5.001, True)])
def test_each_quote_source_age(wired, field, age, stale):
    a, g, c, p, o = setup(wired)
    changed = replace(o, **{field:o.observed_at-timedelta(seconds=age)})
    if stale:
        with pytest.raises(SafetyBlocked, match='STALE_QUOTE'):
            candidate(g, a, changed)
    else:
        candidate(g, a, changed)


def test_naive_time_rejected_and_fresh_quotes_do_not_mutate_hash(wired):
    a, g, c, p, o = setup(wired)
    plan = a.client.transport.authorization.plan
    before = p.digest, plan.digest
    candidate(g, a, replace(o, bid=o.bid + 0))
    assert before == (p.digest, plan.digest)
    with pytest.raises(ValueError, match='UTC'):
        replace(p, expires_at=p.expires_at.replace(tzinfo=None))
    other = replace(p, client_order_id='m05-independent')
    assert other.digest != p.digest
    assert replace(plan, quote_policy=replace(p, client_order_id=plan.client_id_prefix+'new')).digest != plan.digest
