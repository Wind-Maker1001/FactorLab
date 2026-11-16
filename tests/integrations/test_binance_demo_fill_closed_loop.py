"""Intercepted HTTP acceptance; no bar is an exchange execution confirmation."""
from dataclasses import asdict, replace
from decimal import Decimal as D
import json
import os
from pathlib import Path

from test_binance_demo_grid_network import network_grid, observe, writes
from test_binance_demo_network import NetworkFixture, wired

from factorlab.integrations.binance.client import Response

from factorlab.integrations.binance_futures_trading.demo_runtime import open_grid_session
from factorlab.integrations.binance_futures_trading.execution import DemoExecutionGateway
from factorlab.trading import OrderStatus as S, Side
from factorlab.trading.storage import dumps


def economic_events(bridge):
    return [event for _, event in bridge.controller.store.order_events()
            if event.cumulative_filled_quantity]


def test_partial_buy_sell_fees_fifo_equity_and_restart(network_grid, monkeypatch):
    root, store, factory, enable, fake, requests = network_grid
    original = NetworkFixture.__call__

    def marked_account(self, request, timeout):
        response = original(self, request, timeout)
        if request.path not in {'/fapi/v3/account', '/fapi/v3/positionRisk'}:
            return response
        payload = json.loads(response.body)
        pnl = self.quantity * (D('150') - self.entry)
        if request.path == '/fapi/v3/account':
            payload['assets'][0].update(unrealizedProfit=str(pnl), marginBalance=str(self.cash + pnl))
            for position in payload['positions']:
                position['unrealizedProfit'] = str(pnl)
        else:
            for position in payload:
                position.update(markPrice='150', notional=str(self.quantity * D('150')))
        return Response(response.status, response.headers, json.dumps(payload).encode())

    monkeypatch.setattr(NetworkFixture, '__call__', marked_account)
    initialize_gateway = DemoExecutionGateway.__init__

    def estimated_fee(self, *args, **kwargs):
        initialize_gateway(self, *args, **kwargs)
        self.state.fee_rate = D('.007')

    monkeypatch.setattr(DemoExecutionGateway, '__init__', estimated_fee)
    enable()
    evidence = {'label': 'OFFLINE_INTERCEPTED_HTTP_NO_LIVE_MONEY',
                'exchange_messages': [], 'checkpoints': []}

    def checkpoint(bridge, label, inventory, fees, realized):
        account = bridge.controller.account()
        asset, = account.assets
        quantity = sum((p.quantity for p in account.positions), D(0))
        assert account.reconciled and quantity == inventory
        assert asset.total == fake.cash == D('1000') + realized - fees
        unrealized = sum((p.quantity * (p.mark_price - p.entry_price)
                          for p in account.positions), D(0))
        assert unrealized == inventory * D('4')
        buy_remaining = sum((order.intent.quantity - order.cumulative_filled_quantity
                             for order in account.open_orders if order.intent.side is Side.BUY), D(0))
        expected_reserved = inventory * D('146') + buy_remaining * D('150') * D('1.007')
        assert asset.reserved == expected_reserved
        assert asset.available == D('1000') + realized - fees - expected_reserved
        assert asset.total + unrealized == D('1000') + realized + inventory * D('4') - fees
        assert bridge.account().inventory == inventory
        assert bridge.account().equity == asset.total + unrealized
        assert bridge.gateway.adapter.facts.usdt()['unrealizedProfit'] == unrealized
        assert sum((bridge.gateway.adapter.order(key).cumulative_fee
                    for key in fake.orders), D(0)) == fees
        for key, remote in fake.orders.items():
            order = bridge.gateway.adapter.order(key)
            assert order.exchange_order_id == str(remote['orderId'])
            assert order.cumulative_filled_quantity == D(remote['executedQty'])
            assert order.intent.quantity - order.cumulative_filled_quantity == D(remote['origQty']) - D(remote['executedQty'])
        evidence['checkpoints'].append(dict(label=label, account=asdict(account),
            realized_gross=realized, unrealized=unrealized,
            equity=asset.total + unrealized, actual_fees=fees,
            grid=asdict(bridge.account()), orders=bridge.session.status()['orders'],
            exchange_orders=json.loads(json.dumps(fake.orders)),
            exchange_trades=json.loads(json.dumps(fake.trades))))
        return account

    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        assert observe(bridge).status is S.SUBMITTED
        buy = bridge.grid.active[0].intent
        assert buy.side is Side.BUY and buy.limit_price == D('146')
        assert writes(requests)[0]['newClientOrderId'] == [buy.client_order_id]
        assert D(writes(requests)[0]['price'][0]) == D('146')
        checkpoint(bridge, 'BUY_ACK', D(0), D(0), D(0))
        assert not fake.trades and bridge.dispatch() is None
        for index, (delta, cumulative) in enumerate([('.01', '.01'), ('.02', '.03'), ('.01', '.04')]):
            message = fake.trade_update(buy.client_order_id, delta, '146')
            evidence['exchange_messages'].append(message)
            bridge.recover()
            before = checkpoint(bridge, f'BUY_FILL_{index + 1}', D(cumulative), D(cumulative) * D('146') * D('.001'), D(0))
            events = list(bridge.controller.store.order_events())
            bridge.recover()
            assert replace(bridge.controller.account(), observed_at=before.observed_at) == before
            assert list(bridge.controller.store.order_events()) == events
            if cumulative != '.04':
                assert bridge.dispatch() is None
                assert len(writes(requests)) == 1
        assert bridge.dispatch().status is S.SUBMITTED
        sell = bridge.grid.active[0].intent
        assert sell.side is Side.SELL and sell.reduce_only
        assert sell.quantity == D('.04') and sell.limit_price == D('154')
        assert writes(requests)[1]['reduceOnly'] == ['true']
        assert D(writes(requests)[1]['price'][0]) == D('154')
        message = fake.trade_update(sell.client_order_id, '.02', '154')
        evidence['exchange_messages'].append(message)
        bridge.recover()
        checkpoint(bridge, 'SELL_PARTIAL', D('.02'), D('.00892'), D('.16'))
        before_restart = list(bridge.controller.store.order_events())

    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        checkpoint(bridge, 'RESTART_SELL_PARTIAL', D('.02'), D('.00892'), D('.16'))
        assert bridge.gateway.adapter.order(sell.client_order_id).intent == sell
        assert bridge.gateway.adapter.order(buy.client_order_id).intent == buy
        assert bridge.dispatch() is None and len(writes(requests)) == 2
        message = fake.trade_update(sell.client_order_id, '.02', '154')
        evidence['exchange_messages'].append(message)
        bridge.recover()
        checkpoint(bridge, 'SELL_FILLED', D(0), D('.012'), D('.32'))
        assert not bridge.controller.account().open_orders
        assert bridge.controller.account().assets[0].reserved == 0
        assert bridge.account().turnover == D('12')
        events = economic_events(bridge)
        evidence['order_events_before_restart'] = before_restart
        evidence['order_events_final'] = list(bridge.controller.store.order_events())
        # FIFO pairs derive from the confirmed BUY increments (.01, .02, .01)
        # and SELL increments (.02, .02), not from market-bar extrema.
        deltas = []
        previous = {}
        for event in events:
            old_qty, old_fee = previous.get(event.client_order_id, (D(0), D(0)))
            qty = event.cumulative_filled_quantity - old_qty
            fee = event.cumulative_fee - old_fee
            if qty:
                deltas.append((event.client_order_id, qty, event.fill_price, fee))
            previous[event.client_order_id] = (event.cumulative_filled_quantity, event.cumulative_fee)
        assert [(qty, price, fee) for key, qty, price, fee in deltas if key == buy.client_order_id] == [
            (D('.01'), D('146'), D('.00146')),
            (D('.02'), D('146'), D('.00292')),
            (D('.01'), D('146'), D('.00146'))]
        assert [(qty, price, fee) for key, qty, price, fee in deltas if key == sell.client_order_id] == [
            (D('.02'), D('154'), D('.00308')),
            (D('.02'), D('154'), D('.00308'))]
        lots = [[index, qty, price, fee / qty]
                for index, (_, qty, price, fee) in enumerate(
                    (row for row in deltas if row[0] == buy.client_order_id), 1)]
        pairs = []
        for sell_index, (_, remaining, price, fee) in enumerate(
                (row for row in deltas if row[0] == sell.client_order_id), 1):
            sell_fee_rate = fee / remaining
            while remaining:
                lot = lots[0]
                matched = min(remaining, lot[1])
                buy_fee, sell_fee = matched * lot[3], matched * sell_fee_rate
                gross = matched * (price - lot[2])
                pairs.append(dict(buy_fill=lot[0], sell_fill=sell_index, quantity=matched,
                                  buy_fee=buy_fee, sell_fee=sell_fee, gross=gross,
                                  net=gross - buy_fee - sell_fee))
                remaining -= matched
                lot[1] -= matched
                if not lot[1]:
                    lots.pop(0)
        assert not lots
        assert [(row['buy_fill'], row['sell_fill'], row['quantity']) for row in pairs] == [
            (1, 1, D('.01')), (2, 1, D('.01')), (2, 2, D('.01')), (3, 2, D('.01'))]
        assert sum(row['quantity'] for row in pairs) == D('.04')
        assert sum(row['net'] for row in pairs) == bridge.controller.account().assets[0].total - D('1000') == D('.308')
        evidence['execution_deltas'] = deltas
        evidence['fifo_pairs'] = pairs
        evidence['funding'] = dict(authoritative_settlements=[], booked_cashflow='0')
        evidence['local_assumed_fee_rate'] = '.007'
        evidence['local_assumed_fee_booked'] = '0'
        evidence['limit_slippage'] = '0'
        evidence['gross_price_difference'] = '.32'
        final_events = list(bridge.controller.store.order_events())
    with open_grid_session(root, store, factory) as bridge:
        bridge.recover()
        checkpoint(bridge, 'RESTART_COMPLETE', D(0), D('.012'), D('.32'))
        assert list(bridge.controller.store.order_events()) == final_events
        bridge.recover()
        assert list(bridge.controller.store.order_events()) == final_events
    assert len(writes(requests)) == 2 and not writes(requests, 'DELETE')
    assert not [call for call in fake.calls if call[1] == '/fapi/v1/income']
    evidence['http_requests'] = [(request.get_method(), request.full_url.split('?')[0])
                                for request in requests]
    evidence['write_parameters'] = writes(requests)
    for params in evidence['write_parameters']:
        params.pop('signature', None)
        params.pop('timestamp', None)
    destination = os.environ.get('FACTORLAB_M16_EVIDENCE')
    if destination:
        Path(destination).write_text(json.dumps(json.loads(dumps(evidence)), indent=2) + '\n', encoding='utf-8')
