"""Bind a previously journaled strategy without replaying its cash effects twice."""
from decimal import Decimal

from .contracts import Market, decimal_value
from .storage import dumps

ZERO = Decimal(0)


def adopt_allocation(executor, *, account_id, strategy_id, allocation_id, capital,
                     current_cash, official_equity, receipt_id):
    for name, amount in [('allocated capital', capital), ('official equity', official_equity)]:
        decimal_value(amount, name, positive=True)
    decimal_value(current_cash, 'confirmed current cash')
    if not all((account_id, strategy_id, allocation_id, receipt_id)):
        raise ValueError('complete adoption identity required')
    store = executor.store
    identity = 'allocation:' + receipt_id
    payload = dumps(dict(account_id=account_id, strategy_id=strategy_id,
        allocation_id=allocation_id, capital=capital, current_cash=current_cash))
    with store.transaction():
        saved = store.db.execute('SELECT payload FROM capital_migrations WHERE id=?', (identity,)).fetchone()
        if saved is not None:
            if saved[0] != payload:
                raise ValueError('allocation adoption receipt changed')
            return
        if store.allocation(account_id, strategy_id) is not None:
            raise ValueError('strategy already has an allocation')
        if store.db.execute('SELECT 1 FROM portfolio_plans WHERE account=? AND strategy=?',
                            (account_id, strategy_id)).fetchone() is None:
            raise ValueError('existing owner execution journal required')
        executor._recover_account(account_id)
        account = executor.adapter.read_account()
        if not account.reconciled or account.account_id != account_id or executor._account_uncertain(account_id):
            raise ValueError('allocation adoption requires a reconciled account')
        if account.market == Market.BINANCE_USDM:
            raise ValueError('derivative adoption requires original funding evidence through adopt_existing')
        owned = store.holdings(account_id, strategy_id)
        total_owned = store.holdings(account_id)
        actual = {position.instrument_id: position for position in account.positions}
        if any((actual[symbol].quantity if symbol in actual else ZERO) != quantity
               for symbol, quantity in total_owned.items()):
            raise ValueError('existing journal does not reconcile to official inventory')
        value = sum((quantity * actual[symbol].mark_price for symbol, quantity in owned.items() if quantity), ZERO)
        if current_cash + value != capital:
            raise ValueError('adopted capital must equal confirmed cash plus owned marked inventory')
        initial_cash = current_cash - store.cash_flow(account_id, strategy_id)
        if initial_cash < ZERO:
            raise ValueError('existing postings imply a negative initial cash contribution')
        allocated = sum((Decimal(amount) for amount, in store.db.execute(
            'SELECT capital FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
        policy = store.db.execute('SELECT budget FROM capital_accounts WHERE account=?', (account_id,)).fetchone()
        if allocated + capital > official_equity or (policy is not None and allocated + capital > Decimal(policy[0])):
            raise ValueError('adoption exceeds account capital budget')
        cash_used = sum((store.cash(account_id, owner, reserve=False) for owner, in store.db.execute(
            'SELECT strategy FROM portfolio_allocations WHERE account=?', (account_id,))), ZERO)
        official_cash = next(asset.total for asset in account.assets if asset.currency == executor.quote_currency)
        if cash_used + current_cash > official_cash:
            raise ValueError('adoption exceeds official cash')
        store.db.execute('INSERT INTO portfolio_allocations VALUES (?,?,?,?,?)',
                          (account_id, strategy_id, allocation_id, str(capital), str(initial_cash)))
        store.db.execute('INSERT INTO capital_migrations VALUES (?,?)', (identity, payload))
