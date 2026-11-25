from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D, localcontext

import pytest

from factorlab.integrations.binance_futures_trading.account import AccountFacts
from factorlab.trading.capital_apportionment import apportion
from factorlab.trading.capital_funding import attribute_funding
from factorlab.trading.capital_margin import attribute_margin
from factorlab.trading.contracts import Market, OrderEvent, OrderIntent, OrderSnapshot, OrderStatus, OrderType, Side
from factorlab.trading.state import OrderRecord
from factorlab.trading.portfolio_execution import PortfolioStore
from factorlab.trading.storage import dumps
from test_capital_funding import coverage, payment
from test_portfolio_execution import NOW


def shared_store(path):
    store = PortfolioStore(path)
    with store.transaction():
        for owner, quantity in [('one', '.04'), ('two', '.06')]:
            store.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                ('paper', owner, owner, '20', '20'))
            store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                ('paper', 'BTCUSDT', owner, quantity, quantity, '100'))
            store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                (owner, 'paper', owner, 'session', dumps(dict(created_at=NOW))))
    return store


@pytest.mark.parametrize('amount', ['-.1', '.1', '0', '.00000001', '-.00000001'])
def test_shared_historical_funding_conserves_signed_settlements_and_replays(tmp_path, amount):
    path = tmp_path / 'shared.sqlite3'
    when = NOW + timedelta(hours=1)
    event = replace(payment(when, 'BTCUSDT'), amount=D(amount))
    store = shared_store(path)
    first = attribute_funding(store, 'paper', [event, event], coverage(when), when)
    assert first.complete
    assert sum(dict(first.amounts).values()) == event.amount
    if abs(event.amount) == D('.1'):
        assert dict(first.amounts) == {'one': event.amount * D('.4'), 'two': event.amount * D('.6')}
    store.close()
    store = PortfolioStore(path)
    assert attribute_funding(store, 'paper', [event], coverage(when), when) == first
    store.close()


def test_apportion_ties_are_independent_of_input_order():
    assert apportion(D('.00000001'), {'two': D(1), 'one': D(1)}) == (
        ('one', D('.00000001')), ('two', D(0)))
    assert sum(value for _, value in apportion(D('-1.000000001'),
        {'one': D(1), 'two': D(2), 'three': D(3)})) == D('-1.000000001')


def test_reduction_uses_historical_inventory_and_boundary_stays_unresolved(tmp_path):
    store = shared_store(tmp_path / 'history.sqlite3')
    when = NOW + timedelta(hours=1)
    intent = OrderIntent(Market.BINANCE_USDM, 'BTCUSDT', Side.SELL, OrderType.MARKET,
                         D('.02'), None, 'reduce-one')
    record = OrderRecord(OrderSnapshot(intent), when, D(100))
    fill = OrderEvent('reduce-one', 'exchange-one', OrderStatus.FILLED, D('.02'), D(100),
                      when, 'official', 'reduction')
    with store.transaction():
        store.db.execute('INSERT INTO portfolio_orders VALUES (?,?,?)',
                         ('reduce-one', 'one', dumps(dict(record=record))))
        store.db.execute('INSERT INTO portfolio_confirmations VALUES (?,?,?)',
                         ('paper', fill.event_id, dumps(fill)))
        store.db.execute("UPDATE portfolio_claims SET quantity='.02' WHERE strategy='one'")
    before = when - timedelta(minutes=1)
    after = when + timedelta(minutes=1)
    prior = replace(payment(before, 'BTCUSDT'), amount=D('-.1'))
    later = replace(payment(after, 'BTCUSDT'), event_id='later', tran_id='2', amount=D('-.08'))
    result = attribute_funding(store, 'paper', [later, prior], coverage(after), after)
    assert result.complete
    assert dict(result.amounts) == {'one': D('-.06'), 'two': D('-.12')}
    boundary = attribute_funding(store, 'paper', [payment(when, 'BTCUSDT')], coverage(after), after)
    assert not boundary.complete
    assert boundary.unattributed_events == ('funding-1',)
    # A late read does not mutate the frozen plan or re-execute its orders.
    assert store.db.execute("SELECT payload FROM portfolio_plans WHERE id='one'").fetchone()[0] == dumps(dict(created_at=NOW))
    store.close()


def margin_facts(quantity='.1'):
    row = dict(symbol='BTCUSDT', positionSide='BOTH', positionAmt=quantity,
               initialMargin='10', entryPrice='100', unrealizedProfit='0')
    return AccountFacts(account_id='paper', observed_at=NOW, source_complete=True,
        assets=(), positions=(row,), risk=(row,), open_orders=(), trades=(), income=(), hedge_mode=False)


def test_margin_excludes_open_order_margin_and_rejects_conflicting_official_positions(tmp_path):
    from factorlab.errors import DataQualityError
    store = shared_store(tmp_path / 'orders.sqlite3')
    facts = margin_facts()
    row = dict(facts.positions[0], initialMargin='15', positionInitialMargin='10',
               openOrderInitialMargin='5')
    facts = replace(facts, positions=(row,), open_orders=({'symbol': 'BTCUSDT'},))
    assert dict(attribute_margin(store, 'paper', facts).amounts) == {'one': D(4), 'two': D(6)}
    unknown = dict(row)
    del unknown['positionInitialMargin']
    assert not attribute_margin(store, 'paper', replace(facts, positions=(unknown,))).complete
    conflict = dict(facts.risk[0], positionAmt='.2')
    with pytest.raises(DataQualityError, match='conflicting positions'):
        attribute_margin(store, 'paper', replace(facts, risk=(conflict,)))
    store.close()


def test_settlement_aggregation_ignores_ambient_decimal_precision(tmp_path):
    store = shared_store(tmp_path / 'precision.sqlite3')
    facts = margin_facts()
    row = dict(facts.positions[0], initialMargin='1234.56789')
    facts = replace(facts, positions=(row,))
    when = NOW + timedelta(hours=1)
    event = replace(payment(when, 'BTCUSDT'), amount=D('-1234.56789'))
    with localcontext() as context:
        context.prec = 8
        margins = attribute_margin(store, 'paper', facts)
        funding = attribute_funding(store, 'paper', [event], coverage(when), when)
    assert dict(margins.amounts) == {'one': D('493.827156'), 'two': D('740.740734')}
    assert sum(value for _, value in funding.amounts) == event.amount
    assert margins.complete and funding.complete
    store.close()


def test_shared_margin_reconciles_total_not_individual_inventory(tmp_path):
    store = shared_store(tmp_path / 'margin.sqlite3')
    facts = margin_facts()
    result = attribute_margin(store, 'paper', facts)
    assert result.complete and result.estimated
    assert dict(result.amounts) == {'one': D(4), 'two': D(6)}
    assert facts.position_details()[0]['initial_margin'] == D(10)
    mismatch = attribute_margin(store, 'paper', margin_facts('.11'))
    assert not mismatch.complete
    assert mismatch.unattributed_symbols == ('BTCUSDT',)
    assert not attribute_margin(store, 'paper', replace(facts, source_complete=False)).complete
    store.close()
