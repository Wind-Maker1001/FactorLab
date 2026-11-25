"""Official nontrade receipts stay separate from execution-fill ingestion."""
from types import SimpleNamespace
from datetime import datetime, timezone
from decimal import Decimal as D

from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market, Position
from factorlab.trading.portfolio_execution import PortfolioExecutor

import pytest

from factorlab.integrations.alpaca.adapter import AlpacaAdapter
from factorlab.integrations.alpaca.corporate_actions import CorporateReceiptJournal
from factorlab.trading.portfolio_execution import PortfolioStore


def test_nontrade_activity_pagination_preserves_filters_and_dividend_eligibility():
    calls = []
    first = [dict(id=str(i), activity_type='SSP', symbol='AAPL') for i in range(100)]
    dividend = dict(id='dividend', activity_type='DIV', symbol='AAPL',
                    qty='10', net_amount='2.50', per_share_amount='.25', date='2026-11-24')

    def request(method, path, *, params):
        calls.append((method, path, dict(params)))
        assert 'activity_types' not in params
        return first if 'page_token' not in params else [
            dict(id='fill', activity_type='FILL'), dividend,
            dict(id='cash', activity_type='CSD')]

    adapter = object.__new__(AlpacaAdapter)
    adapter.http = SimpleNamespace(request=request)
    result = adapter.read_corporate_activities(after='2026-11-23T00:00:00Z')
    assert len(result) == 101
    assert result[-1] == dividend
    assert calls[0][1] == calls[1][1] == '/v2/account/activities'
    assert calls[1][2]['page_token'] == '99'
    for _, _, params in calls:
        assert params['after'] == '2026-11-23T00:00:00Z'
        assert 'activity_types' not in params
        assert params['direction'] == 'asc'


def test_official_split_batch_and_dividend_post_without_changing_dividend_shares(tmp_path):
    store = PortfolioStore(tmp_path / 'portfolio.sqlite3')
    for symbol in ('AAPL', 'QQQ', 'GE'):
        store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
                         ('paper', symbol, 'v69', '10', '10', '100'))
    account = AccountSnapshot(Market.ALPACA_US_EQUITY, 'paper',
        (AssetBalance('USD', D(100), D(0)),),
        tuple(Position(symbol, quantity, D(100), D(100), 'USD')
              for symbol, quantity in [('AAPL', D(40)), ('QQQ', D(10)), ('GE', D(10))]),
        (), datetime(2026, 11, 24, tzinfo=timezone.utc), True)
    executor = PortfolioExecutor(SimpleNamespace(read_account=lambda: account), store)
    journal = CorporateReceiptJournal(store, 'paper')
    announcements = [dict(id=f'ann-{day}', ca_type='split', initiating_symbol='AAPL',
                          ex_date=day, old_rate='1', new_rate='2')
                     for day in ('2026-11-23', '2026-11-24')]
    receipts = [dict(id=f'ssp-{day}', activity_type='SSP', symbol='AAPL',
                     date=day, net_amount='0', qty='10')
                for day in ('2026-11-23', '2026-11-24')]
    receipts += [dict(id='div', activity_type='DIV', symbol='QQQ',
                      date='2026-11-24', net_amount='2.5', qty='10'),
                 dict(id='spin', activity_type='SSO', symbol='GE',
                      date='2026-11-24', net_amount='0', qty='1')]
    for _ in range(2):
        blocked = journal.reconcile(executor, strategy_id='v69',
                                    announcements=announcements, receipts=receipts)
        assert blocked == {'GE': 'OFFICIAL_CORPORATE_ALLOCATION_REQUIRED'}
    claims = dict(store.db.execute('SELECT symbol,quantity FROM portfolio_claims'))
    assert D(claims['AAPL']) == 40
    assert D(claims['QQQ']) == 10
    actions = store.db.execute('SELECT payload FROM portfolio_corporate_actions').fetchall()
    assert len(actions) == 3
    assert journal.read('spin') == receipts[-1]
    store.close()


def test_receipts_survive_restart_without_interpreting_dividend_qty(tmp_path):
    path = tmp_path / 'portfolio.sqlite3'
    receipt = dict(id='official-DIV', activity_type='DIV', symbol='AAPL',
                   qty='10', net_amount='2.50', date='2026-11-24')
    store = PortfolioStore(path)
    CorporateReceiptJournal(store, 'paper').ingest([receipt])
    store.close()
    store = PortfolioStore(path)
    journal = CorporateReceiptJournal(store, 'paper')
    journal.ingest([receipt])
    assert journal.read(receipt['id']) == receipt
    assert store.db.execute('SELECT COUNT(*) FROM portfolio_claims').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM portfolio_corporate_actions').fetchone()[0] == 0
    with pytest.raises(ValueError, match='different payload'):
        journal.ingest([dict(id='new-spin', activity_type='SSO', symbol='CHILD', qty='2'),
                        dict(receipt, net_amount='3.00')])
    assert journal.read('new-spin') is None
    assert journal.read(receipt['id']) == receipt
    store.close()
