"""Initialize independently authorized Demo capital without adopting legacy economics."""
from dataclasses import asdict
from decimal import Decimal
import json

from factorlab.trading.storage import decode
from .protocol import SafetyBlocked

SOURCE_KEY = 'demo:fresh-capital:p10-12:v1'
CAPITAL = Decimal(20)
LEGACY_CAPITAL = Decimal(27)


def source(session, store=None):
    raw = (session.controller.store if store is None else store).event_payload(SOURCE_KEY)
    return None if raw is None else decode(json.loads(raw))


def initialize(session, executor, *, allocations, symbols, fee_rate):
    """Record a fixed NEW20 cash baseline; retain the original 7/20 commitments."""
    account_id = session.adapter.account_id
    declaration = dict(account_id=account_id, capital=CAPITAL,
        retained_legacy_capital=LEGACY_CAPITAL, allocations=allocations,
        symbols=sorted(symbols), fee_rate=fee_rate,
        risk_limits=asdict(session.controller.limits))
    previous = source(session, executor.store)
    if previous is not None:
        if previous['declaration'] != declaration:
            raise SafetyBlocked('fresh capital identity or risk limits changed')
        session.controller.store.record_metadata(SOURCE_KEY, previous)
        return previous
    if (not allocations or len({row['instance_id'] for row in allocations}) != len(allocations)
            or len({row['allocation_id'] for row in allocations}) != len(allocations)
            or any(not row['instance_id'] or not row['allocation_id'] or row['capital'] <= 0 for row in allocations)
            or sum((row['capital'] for row in allocations), Decimal(0)) != CAPITAL):
        raise SafetyBlocked('independent allocations must total exactly 20 USDT')
    shared = session.controller.store.event_payload('demo:static-shared-account:v1')
    if shared is None:
        raise SafetyBlocked('original shared account allocation declaration required')
    shared = decode(json.loads(shared))
    if shared['grid_budget'] != Decimal(7) or shared['portfolio_budget'] != Decimal(20):
        raise SafetyBlocked('original grid and legacy portfolio commitments changed')
    account = session.gateway.read_account()
    funds = session.adapter.read_facts().usdt()
    if (not account.reconciled or account.positions or account.open_orders
            or funds['availableBalance'] < CAPITAL + LEGACY_CAPITAL):
        raise SafetyBlocked('fresh baseline requires flat reconciled account and unencumbered 47 USDT cash')
    store = executor.store
    expected = {row['instance_id']: dict(allocation_id=row['allocation_id'],
        capital=row['capital'], initial_cash=row['capital']) for row in allocations}
    for owner, in store.db.execute('SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,)):
        if owner not in expected or store.allocation_basis(account_id, owner) != expected[owner]:
            raise SafetyBlocked('fresh baseline conflicts with existing public capital')
    receipt = dict(declaration=declaration, observed_at=account.observed_at,
        official_wallet=funds['walletBalance'], official_available=funds['availableBalance'],
        historical_capital_migration='INCOMPLETE')
    with store.transaction():
        store.capital_policy.configure(account_id=account_id, currency='USDT', budget=CAPITAL,
            fee_rate=fee_rate, mode='SHARED_SAME_DIRECTION',
            symbol_limits={symbol: min(CAPITAL, session.controller.limits.max_instrument_exposure)
                           for symbol in symbols})
        for row in allocations:
            executor.allocate(account_id=account_id, strategy_id=row['instance_id'],
                allocation_id=row['allocation_id'], capital=row['capital'], initial_cash=row['capital'],
                official_equity=funds['walletBalance'] + funds['unrealizedProfit'])
            store.capital_policy.bind(account_id=account_id, strategy_id=row['instance_id'],
                instance_id=row['instance_id'], allocation_id=row['allocation_id'],
                logical_strategy=row['strategy_id'])
        store.record_metadata(SOURCE_KEY, receipt)
    session.controller.store.record_metadata(SOURCE_KEY, receipt)
    return receipt
