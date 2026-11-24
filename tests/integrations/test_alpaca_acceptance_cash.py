"""Cash baselines must not absorb fills from earlier acceptance campaigns."""
from decimal import Decimal as D
from types import SimpleNamespace as NS

from factorlab.integrations.alpaca.portfolio_acceptance import campaign_cash_delta
from factorlab.trading.contracts import Side


def test_campaign_cash_excludes_predecessor_and_includes_rotation_fees():
    class Store:
        db = NS(execute=lambda _: NS(fetchall=lambda: [('old',), ('open',), ('rotate',), ('prefix',)]))

        def plan(self, identity):
            campaign = {'old': 'ALP-E03 TEST', 'prefix': 'ALP-E04 CLOUD TEST EXTRA'}.get(
                identity, 'ALP-E04 CLOUD TEST')
            return {'request': {'rebalance_id': campaign + ':uuid:' + identity}}

        def orders(self, identity):
            return [{'record': NS(snapshot=NS(intent=NS(client_order_id=identity)))}]

    def fill(identity, side, notional, fee='0'):
        return NS(client_order_id=identity, side=side, notional=D(notional), fee=D(fee))

    fills = [fill('old', Side.BUY, '100'), fill('old', Side.SELL, '101'),
             fill('open', Side.BUY, '20'), fill('rotate', Side.SELL, '5', '.01'),
             fill('rotate', Side.BUY, '5.1', '.02'), fill('prefix', Side.BUY, '999')]
    assert campaign_cash_delta(Store(), 'ALP-E04 CLOUD TEST', fills) == D('-20.13')
