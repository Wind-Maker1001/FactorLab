from decimal import Decimal as D
import sqlite3

import pytest

from factorlab.trading.capital import CapitalPolicy, CapitalPolicyMode
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from test_capital_policy import setup
from test_portfolio_allocations import allocate, plan
from test_portfolio_execution import Broker, NOW


SHARED = CapitalPolicyMode.SHARED_SAME_DIRECTION


def share(policy):
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0), mode=SHARED)


def claim(store, owner, symbol='AAPL'):
    with store.transaction():
        store.capital_policy.claim('paper', owner, symbol)


def test_shared_claims_preserve_identity_and_monotonic_symbol_limit(tmp_path):
    broker, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    share(policy)
    claim(store, 'two')
    assert policy.symbol_limit('paper', 'AAPL') == D(60)
    claim(store, 'one')
    assert policy.symbol_limit('paper', 'AAPL') == D(40)
    owners = policy.ownership('paper', 'AAPL')
    assert [(o.instance_id, o.allocation_id, o.logical_strategy_id) for o in owners] == [
        ('one', 'one-cash', 'one'), ('two', 'two-cash', 'two')]
    with store.transaction():
        policy.release('paper', 'one', 'AAPL')
    assert policy.symbol_limit('paper', 'AAPL') == D(40)
    policy.set_symbol_limit('paper', 'AAPL', D(30))
    with pytest.raises(ValueError, match='cannot increase'):
        policy.set_symbol_limit('paper', 'AAPL', D(31))
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    policy = store.capital_policy
    policy.configure(account_id='paper', currency='USD', budget=D(100), fee_rate=D(0))
    assert policy.mode('paper') == SHARED
    assert policy.symbol_limit('paper', 'AAPL') == D(30)
    assert [o.instance_id for o in policy.ownership('paper')] == ['two']
    assert policy.snapshot('paper', 'one').allocation_id == 'one-cash'
    store.close()


def test_shared_claim_rejects_unbound_owner_and_identity_reuse(tmp_path):
    _, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    share(policy)
    with pytest.raises(ValueError, match='binding required'):
        claim(store, 'unfunded')
    with pytest.raises(ValueError, match='binding changed'):
        policy.bind(account_id='paper', strategy_id='one', instance_id='one',
                    allocation_id='one-cash', logical_strategy='another-package')
    assert policy.ownership('paper') == ()
    store.close()


def legacy_schema(store):
    with store.transaction():
        store.db.execute('ALTER TABLE capital_accounts DROP COLUMN mode')
        store.db.execute('ALTER TABLE capital_symbols RENAME TO current_symbols')
        store.db.execute('CREATE TABLE capital_symbols(account TEXT NOT NULL,symbol TEXT NOT NULL,'
                         'strategy TEXT NOT NULL,PRIMARY KEY(account,symbol))')
        store.db.execute('INSERT INTO capital_symbols SELECT * FROM current_symbols')
        store.db.execute('DROP TABLE current_symbols')
        store.db.execute('DROP TABLE capital_symbol_limits')


def test_legacy_schema_upgrade_preserves_ownership_and_defaults_exclusive(tmp_path):
    _, store, _ = setup(tmp_path)
    claim(store, 'one')
    legacy_schema(store)
    store.close()
    store = PortfolioStore(tmp_path / 'capital.sqlite3')
    policy = store.capital_policy
    assert policy.mode('paper') == CapitalPolicyMode.EXCLUSIVE
    assert policy.ownership('paper')[0].allocation_id == 'one-cash'
    with pytest.raises(ValueError, match='ownership conflict'):
        claim(store, 'two')
    share(policy)
    claim(store, 'two')
    assert len(policy.ownership('paper')) == 2
    store.close()


def test_schema_upgrade_failure_rolls_back_ddl_and_account_mode(tmp_path):
    _, store, _ = setup(tmp_path)
    claim(store, 'one')
    legacy_schema(store)
    def authorize(action, table, *_):
        if action == sqlite3.SQLITE_DROP_TABLE and table == 'capital_symbols_exclusive':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.db.set_authorizer(authorize)
    with pytest.raises(sqlite3.DatabaseError):
        CapitalPolicy(store)
    store.db.set_authorizer(None)
    assert 'mode' not in {row[1] for row in store.db.execute('PRAGMA table_info(capital_accounts)')}
    assert store.db.execute('SELECT * FROM capital_symbols').fetchall() == [('paper', 'AAPL', 'one')]
    assert [row[1] for row in store.db.execute('PRAGMA table_info(capital_symbols)') if row[5]] == ['account', 'symbol']
    CapitalPolicy(store)
    store.close()


def test_shared_mode_adopts_funded_inventory_but_rejects_opposite_owners(tmp_path):
    _, store, executor = setup(tmp_path)
    with store.transaction():
        for owner, quantity in [('one', '1'), ('two', '2')]:
            store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                             ('paper', 'AAPL', owner, quantity, quantity, '10'))
    share(executor.capital_policy)
    claim(store, 'one')
    claim(store, 'two')
    with pytest.raises(ValueError, match='shared symbols'):
        executor.capital_policy.configure(account_id='paper', currency='USD', budget=D(100),
                                         fee_rate=D(0), mode=CapitalPolicyMode.EXCLUSIVE)
    assert executor.capital_policy.mode('paper') == SHARED
    with pytest.raises(ValueError, match='flat instance'):
        with store.transaction():
            executor.capital_policy.release('paper', 'one', 'AAPL')
    with store.transaction():
        store.db.execute("UPDATE portfolio_claims SET quantity='-2' WHERE strategy='two'")
    with pytest.raises(ValueError, match='direction conflict'):
        claim(store, 'one')
    store.close()


def test_qualify_failure_rolls_back_all_claims_binding_and_tighter_limit(tmp_path):
    from types import SimpleNamespace
    _, store, executor = setup(tmp_path)
    policy = executor.capital_policy
    claim(store, 'two', 'BLOCKED')
    spec = SimpleNamespace(capital={'currency': 'USD', 'requested': D(40)},
        symbols=['AAPL', 'BLOCKED'], account_id='paper', market='BINANCE_EQUITY', strategy_id='one')
    binding = dict(account_id='paper', instance_id='one', strategy_id='one',
                   market='BINANCE_EQUITY', allocation_id='one-cash')
    with pytest.raises(ValueError, match='ownership conflict'):
        policy.qualify(spec, binding)
    assert [o.symbol for o in policy.ownership('paper')] == ['BLOCKED']
    with pytest.raises(KeyError):
        policy.symbol_limit('paper', 'AAPL')
    store.close()
