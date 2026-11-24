"""Optional ALP-E03 working-tree contract test (no broker networking)."""
import os
from pathlib import Path
import importlib.util
from decimal import Decimal as D
import pytest


def test_e03_exact_share_bridge(tmp_path):
    root=os.environ.get('FACTORLAB_ALP_E03_SRC')
    if not root: pytest.skip('ALP-E03 dependency branch not in main; set FACTORLAB_ALP_E03_SRC')
    import factorlab.trading
    factorlab.trading.__path__.append(str(Path(root)/'factorlab/trading'))
    try:
        from factorlab.trading.portfolio_execution import PortfolioExecutor,PortfolioStore
        spec=importlib.util.spec_from_file_location('e03_test_broker',Path(root).parent/'tests/trading/test_portfolio_execution.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        from factorlab.strategies.v69 import ASSETS
        from factorlab.strategies.v69_alpaca import create_alpaca_plan
        broker=module.Broker(cash=D(100000))
        store=PortfolioStore(tmp_path/'e03.sqlite')
        executor=PortfolioExecutor(broker,store,quote_currency='USD',clock=lambda:module.NOW)
        decision=dict(weights=dict.fromkeys(ASSETS,.04),orders=[dict(symbol='AAPL',side='buy',adjusted_quantity=2.25)])
        args=dict(decision=decision,account_id='paper',session='2026-11-24',now=module.NOW,
                  raw_holdings={},factors=dict.fromkeys(ASSETS,.5),quantity_steps=dict.fromkeys(ASSETS,.01))
        identity=create_alpaca_plan(executor,**args)
        assert create_alpaca_plan(executor,**args)==identity
        assert store.plan(identity)['targets']['AAPL']==D('1.12')
        module.run(executor,identity)
        assert len(broker.posts)==1
        assert broker.posts[0].quantity==D('1.12')
        module.run(executor,identity)
        assert len(broker.posts)==1
        store.close()
    finally:
        factorlab.trading.__path__.remove(str(Path(root)/'factorlab/trading'))
