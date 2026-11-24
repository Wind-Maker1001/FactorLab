from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading import demo_portfolio_consumer as module
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked
from factorlab.trading.control import RiskLimits


def test_portfolio_loss_baseline_survives_runtime_restart(tmp_path, monkeypatch):
    funds = dict(walletBalance=D(5000), unrealizedProfit=D(0))
    facts = SimpleNamespace(usdt=lambda:funds)
    adapter = SimpleNamespace(account_id='DEMO:FactorLab-Demo:unit', facts=facts,
                              read_facts=lambda:facts)
    state = SimpleNamespace(orders={}, positions={})
    session = SimpleNamespace(symbol='NVDAUSDT',configuration={},
        gateway=SimpleNamespace(rules=object(),state=state),adapter=adapter,
        client=SimpleNamespace(transport=object()),_write_guard=None,_receipt_sink=None,
        _admit_write=lambda *args:None,controller=SimpleNamespace(limits=RiskLimits()),
        _now=lambda:datetime.now(timezone.utc))
    monkeypatch.setattr(module,'demo_account_configuration',lambda *args:{'leverage':1})
    monkeypatch.setattr(module,'observe_demo_rules',lambda *args,**kwargs:SimpleNamespace(rules=object()))
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    params = dict(symbol='QQQUSDT',side='BUY',reduceOnly='false',quantity='.01',price='600',
                  newClientOrderId='pf-first')
    with consumer.scope('stable-task','QQQUSDT'):
        session._write_guard('POST',params,session.client.transport.authorization.plan)
    runtime.close()
    funds['walletBalance'] -= 3
    runtime = DemoRuntime(tmp_path)
    consumer = module.DemoPortfolioConsumer(session,runtime,tmp_path)
    with consumer.scope('stable-task','QQQUSDT'):
        with pytest.raises(SafetyBlocked,match='persistent account loss limit'):
            session._write_guard('POST',dict(params,newClientOrderId='pf-second'),
                                 session.client.transport.authorization.plan)
    assert runtime.db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 1
    runtime.close()
