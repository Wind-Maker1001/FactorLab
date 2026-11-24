"""Configured symbols are capabilities, not proof of exchange tradability."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from factorlab.integrations.binance_futures_trading.demo_preflight import _book
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.network import DemoExecutionPlan
from factorlab.errors import DataQualityError


def test_configured_book_rejects_cross_symbol():
    payload = dict(symbol='QQQUSDT', bidPrice='500', askPrice='501', time=int(NOW.timestamp()*1000))
    assert _book(payload, 'QQQUSDT')['askPrice'] == D(501)
    with pytest.raises(DataQualityError, match='identity'):
        _book(payload, 'SPYUSDT')
    with pytest.raises(DataQualityError, match='identity'):
        _book(payload)


def test_configured_plan_preserves_account_limits():
    from datetime import timedelta
    args = dict(account_id='DEMO:FactorLab-Demo:test', order_types=frozenset({'LIMIT'}),
        max_quantity=D(1), max_notional=D(10), max_orders=2, max_open_orders=1,
        max_seconds=60, max_loss=D(1), max_margin=D(10), expires_at=NOW+timedelta(seconds=60),
        client_id_prefix='qqq', cleanup='CANCEL_KNOWN_THEN_STOP', recovery='GET_RECONCILE_NO_RESUBMIT')
    plan = DemoExecutionPlan(symbol='QQQUSDT', **args)
    assert plan.symbol == 'QQQUSDT'
    assert replace(plan, symbol='SPYUSDT').max_margin == plan.max_margin
    assert DemoExecutionPlan(**args).symbol == 'NVDAUSDT'
