"""Public E03 contract; no external worktree or broker requests."""
from datetime import datetime, timezone
from decimal import Decimal as D
import pytest
from factorlab.strategies.v69 import ASSETS
from factorlab.strategies.v69_alpaca import create_alpaca_plan

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


def arguments():
    return dict(decision=dict(weights=dict.fromkeys(ASSETS,.04),
        orders=[dict(symbol='AAPL',side='buy',adjusted_quantity=2.25)],
        state={'last_week':[2026,48]}), account_id='paper',session='2026-11-24',now=NOW,
        raw_holdings={},factors=dict.fromkeys(ASSETS,.5),quantity_steps=dict.fromkeys(ASSETS,.01))


class PublicExecutor:
    # Deliberately exposes only the supported public create API.
    def create(self, *, account_id, strategy_id, strategy_version, trading_session,
               weights, owned_symbols, now, initial_inventory, economic_multipliers,
               quantity_deltas, signal_inputs):
        self.deltas, self.inputs = quantity_deltas, signal_inputs
        return 'plan-id'


def test_public_quantity_contract():
    executor=PublicExecutor()
    assert create_alpaca_plan(executor,**arguments())=='plan-id'
    assert executor.deltas['AAPL']==D('1.12')
    assert all(executor.deltas[s]==0 for s in ASSETS if s!='AAPL')
    assert executor.inputs['checkpoint']=={'last_week':[2026,48]}
    assert executor.inputs['kind']=='V69_NATURAL'


def test_natural_flat_signal_stays_flat():
    args=arguments();args['decision']['weights']=dict.fromkeys(ASSETS,0.)
    args['decision']['orders']=[]
    executor=PublicExecutor();create_alpaca_plan(executor,**args)
    assert all(q==0 for q in executor.deltas.values())


def test_e03_exact_share_bridge(tmp_path):
    # Direct dependency becomes active in ordinary CI once E03 is merged.
    module=pytest.importorskip('factorlab.trading.portfolio_execution',
        reason='E03 public module not yet merged into main')
    with_store=module.PortfolioStore(tmp_path/'e03.sqlite')
    try:
        executor=module.PortfolioExecutor(None,with_store,quote_currency='USD')
        args=arguments()
        identity=create_alpaca_plan(executor,**args)
        assert create_alpaca_plan(executor,**args)==identity
        plan=with_store.plan(identity)
        assert plan['targets']['AAPL']==D('1.12')
        assert plan['request']['signal_inputs']['checkpoint']==args['decision']['state']
        assert plan['request']['quantity_deltas']['AAPL']==D('1.12')
        args['decision']['state']={'last_week':[2026,49]}
        with pytest.raises(ValueError):create_alpaca_plan(executor,**args)
    finally:
        with_store.close()
