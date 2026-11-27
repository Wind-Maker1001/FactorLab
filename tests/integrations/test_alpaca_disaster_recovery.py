from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from factorlab.integrations.alpaca.cloud_runtime import RuntimeStore
from factorlab.integrations.alpaca.disaster_recovery import RecoveryHTTP, recover_paper
from factorlab.integrations.alpaca.http import AlpacaConfig
from factorlab.integrations.alpaca.portfolio import AlpacaPortfolioRunner
from factorlab.integrations.alpaca.storage import AlpacaStore
from factorlab.trading.contracts import Market, OrderStatus
from factorlab.trading.portfolio_execution import PortfolioStore

from test_alpaca_portfolio import Adapter, Missing, NOW, create, inputs


class PaperFixture(Adapter):
    def read_account(self):
        return replace(super().read_account(), market=Market.ALPACA_US_EQUITY)


def test_restore_recovers_original_lost_post_and_later_partial_fill(tmp_path):
    portfolio_path = tmp_path / 'portfolio.sqlite3'
    store = PortfolioStore(portfolio_path)
    paper = PaperFixture()
    runner = AlpacaPortfolioRunner(paper, store, order_not_found=Missing, clock=lambda: NOW)
    plan_id = create(runner)
    paper.die_after_post = True
    with pytest.raises(SystemExit):
        runner.run_once(plan_id, **inputs())
    original = paper.posts[0]
    store.close()

    with_cash = RuntimeStore(tmp_path)
    cash_window_end = NOW + timedelta(seconds=2)
    with_cash.report(cash_window_end, {
        'source': 'ALPACA_PAPER_ACCOUNT', 'cash': '100',
        'cash_request_started_at': (NOW + timedelta(seconds=1)).isoformat(),
        'cash_observed_at': cash_window_end.isoformat(),
    })
    with_cash.db.close()
    account = AlpacaStore(tmp_path / 'account.sqlite3')
    account.put('account_id', 'paper')
    account.admit(original, original.limit_price)
    account.close()

    # The broker formed the USD 100 response before a fill arrived in flight.
    execution_at = (cash_window_end - timedelta(milliseconds=500)).isoformat()
    order = dict(id='broker-order', client_order_id=original.client_order_id, symbol='AAPL', side='buy',
                 type='limit', qty=str(original.quantity), time_in_force=original.time_in_force.lower(),
                 limit_price=str(original.limit_price), status='partially_filled', filled_qty='1',
                 filled_avg_price='10', created_at=(NOW - timedelta(seconds=5)).isoformat(),
                 submitted_at=(NOW - timedelta(seconds=5)).isoformat(), updated_at=execution_at)
    fills = [dict(id='fill-a', order_id=order['id'], qty='1', price='10',
                  symbol='AAPL', side='buy', transaction_time=execution_at)]
    cash = Decimal(90)

    def transport(method, path, params, body):
        assert method == 'GET'
        if path == '/v2/account':
            return 200, dict(id='paper', currency='USD', equity='100', cash=str(cash), buying_power=str(cash),
                             status='ACTIVE', trading_blocked=False, account_blocked=False)
        if path == '/v2/positions':
            return 200, [dict(symbol='AAPL', qty=order['filled_qty'], avg_entry_price='10', current_price='10')]
        if path == '/v2/account/activities/FILL':
            assert 'after' in params
            return 200, fills
        if path == '/v2/account/activities':
            return 200, []
        if path == '/v2/orders:by_client_order_id':
            assert params['client_order_id'] == original.client_order_id
            return 200, order
        if path == '/v2/orders':
            return 200, [order] if params['status'] == 'open' and order['status'] == 'partially_filled' else []
        raise AssertionError(path)

    http = RecoveryHTTP(AlpacaConfig('paper-key', 'paper-secret'), transport=transport)
    first = recover_paper(tmp_path, http, since=NOW)
    assert first['reconciled'], first
    store = PortfolioStore(portfolio_path)
    partial = store.orders(plan_id)[0]['record'].snapshot
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.cumulative_filled_quantity == Decimal(1)
    store.close()

    later_at = (cash_window_end + timedelta(seconds=1)).isoformat()
    order.update(status='filled', filled_qty=str(original.quantity), updated_at=later_at)
    fills.append(dict(fills[0], id='fill-b', qty=str(original.quantity - 1), transaction_time=later_at))
    cash = 100 - original.quantity * 10
    result = recover_paper(tmp_path, http, since=NOW)
    assert result['reconciled'], result
    assert result == recover_paper(tmp_path, http, since=NOW)
    store = PortfolioStore(portfolio_path)
    filled = store.orders(plan_id)[0]
    assert filled['record'].snapshot.status is OrderStatus.FILLED
    assert filled['record'].snapshot.cumulative_filled_quantity == original.quantity
    assert filled['filled_notional'] == original.quantity * 10
    assert store.holdings('paper', 'ALP-E03 TEST') == {'AAPL': original.quantity}
    store.close()
