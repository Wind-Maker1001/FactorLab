"""Deterministic NVDAUSDT protocol fixture, not another market client/engine."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import parse_qs

from factorlab.integrations.binance.client import Response
from .protocol import OfflineTransport
from .rules import RuleObservation

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
MS = int(NOW.timestamp() * 1000)


def synthetic_rules():
    return RuleObservation.from_exchange(
        {'symbol': 'NVDAUSDT', 'status': 'TRADING', 'quoteAsset': 'USDT', 'marginAsset': 'USDT', 'orderTypes': ['MARKET', 'LIMIT'], 'filters': [
            {'filterType': 'LOT_SIZE', 'minQty': '0.01', 'maxQty': '100', 'stepSize': '0.01'},
            {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.01', 'maxQty': '100', 'stepSize': '0.01'},
            {'filterType': 'PRICE_FILTER', 'minPrice': '0.01', 'maxPrice': '10000', 'tickSize': '0.01'},
            {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]},
        {'symbol': 'NVDAUSDT', 'brackets': [{'bracket': 1, 'initialLeverage': 5,
             'notionalFloor': '0', 'notionalCap': '10000', 'maintMarginRatio': '0.01', 'cum': '0'}]},
        NOW, 'SYNTHETIC_TEST_FIXTURE_V1_NOT_EXCHANGE_RULES')


class FakeTransport(OfflineTransport):
    inject_responses_on_get = True

    def __init__(self):
        self.calls, self.orders, self.trades = [], {}, []
        self.income = None
        self.cash, self.quantity, self.entry = Decimal('1000'), Decimal('0'), Decimal('0')
        self.timeout_next, self.response_next = False, None
        self.hedge_mode = False

    def __call__(self, request, timeout):
        self.calls.append((request.method, request.path, parse_qs(request.query)))
        p = {k: v[0] for k, v in parse_qs(request.query).items()}
        if self.response_next is not None and (request.method != 'GET' or self.inject_responses_on_get):
            response, self.response_next = self.response_next, None
            return response
        if request.path == '/fapi/v1/time':
            value = {'serverTime': MS}
        elif request.path == '/fapi/v1/order':
            key = p.get('newClientOrderId', p.get('origClientOrderId'))
            if request.method == 'POST':
                if key in self.orders:
                    return Response(400, {}, b'{"code":-4116}')
                self.orders[key] = dict(clientOrderId=key, orderId=len(self.orders) + 1,
                    symbol=p['symbol'], side=p['side'], type=p['type'], positionSide=p['positionSide'],
                    origQty=p['quantity'], executedQty='0', cumQuote='0', status='NEW', updateTime=MS)
                if self.timeout_next:
                    self.timeout_next = False
                    raise TimeoutError('synthetic accepted order reply lost')
            if key not in self.orders:
                return Response(400, {}, b'{"code":-2013}')
            value = self.orders[key]
            if request.method == 'DELETE' and value['status'] != 'FILLED':
                value['status'] = 'CANCELED'
                value['updateTime'] += 1
        elif request.path in {'/fapi/v1/openOrders', '/fapi/v1/allOrders'}:
            value = [dict(o) for o in self.orders.values() if request.path.endswith('allOrders') or o['status'] in {'NEW', 'PARTIALLY_FILLED'}]
        elif request.path == '/fapi/v1/userTrades':
            value = [t for t in self.trades if 'orderId' not in p or str(t['orderId']) == p['orderId']]
        elif request.path == '/fapi/v1/income':
            if self.income is None:
                rows = [{'symbol': 'NVDAUSDT', 'incomeType': 'FUNDING_FEE', 'income': '-0.02',
                         'asset': 'USDT', 'time': MS, 'tranId': 1}]
            else:
                rows = [dict(row) for row in self.income]
            if 'symbol' in p:
                rows = [row for row in rows if row.get('symbol') == p['symbol']]
            if 'incomeType' in p:
                rows = [row for row in rows if row.get('incomeType') == p['incomeType']]
            if 'startTime' in p:
                start = int(p['startTime'])
                rows = [row for row in rows if isinstance(row.get('time'), int) and row['time'] >= start]
            if 'endTime' in p:
                end = int(p['endTime'])
                rows = [row for row in rows if isinstance(row.get('time'), int) and row['time'] <= end]
            rows.sort(key=lambda row: (row.get('time', 0) if isinstance(row.get('time'), int) else 0,
                                       str(row.get('tranId', ''))))
            limit = int(p.get('limit', 100))
            value = rows[:limit]
        elif request.path == '/fapi/v1/positionSide/dual':
            value = {'dualSidePosition': self.hedge_mode}
        elif request.path == '/fapi/v3/account':
            collateral = abs(self.quantity) * self.entry
            value = {'assets': [{'asset': 'USDT', 'walletBalance': str(self.cash),
                      'availableBalance': str(self.cash - collateral), 'marginBalance': str(self.cash),
                      'initialMargin': str(collateral), 'maintMargin': str(collateral * Decimal('0.01')),
                      'unrealizedProfit': '0'}],
                     'positions': [] if not self.quantity else [{'symbol': 'NVDAUSDT', 'positionSide': 'BOTH',
                         'positionAmt': str(self.quantity), 'entryPrice': str(self.entry),
                         'unrealizedProfit': '0', 'initialMargin': str(collateral), 'maintMargin': str(collateral * Decimal('0.01'))}]}
        elif request.path == '/fapi/v3/positionRisk':
            value = [] if not self.quantity else [{'symbol': 'NVDAUSDT', 'positionSide': 'BOTH',
                       'positionAmt': str(self.quantity), 'entryPrice': str(self.entry),
                       'markPrice': str(self.entry), 'notional': str(self.quantity * self.entry),
                       'liquidationPrice': '0', 'isolatedMargin': '0'}]
        elif request.path == '/fapi/v1/symbolConfig':
            value = [{'symbol': 'NVDAUSDT', 'leverage': 1, 'marginType': 'CROSSED'}]
        elif request.path == '/fapi/v1/leverageBracket':
            value = [{'symbol': 'NVDAUSDT', 'brackets': list(synthetic_rules().brackets)}]
        else:
            raise ValueError('unsupported fixture request')
        return Response(200, {}, json.dumps(value).encode())

    def trade_update(self, key, quantity='1', price='100'):
        row = self.orders[key]
        qty, price = Decimal(quantity), Decimal(price)
        filled = Decimal(row['executedQty']) + qty
        if qty <= 0 or filled > Decimal(row['origQty']):
            raise ValueError('invalid synthetic trade')
        quote = Decimal(row['cumQuote']) + qty * price
        fee = qty * price * Decimal('0.001')
        signed = qty if row['side'] == 'BUY' else -qty
        if self.quantity * signed < 0:
            self.cash += min(abs(self.quantity), qty) * (price - self.entry) * (1 if self.quantity > 0 else -1)
        new_quantity = self.quantity + signed
        if self.quantity * signed >= 0:
            self.entry = (abs(self.quantity) * self.entry + qty * price) / abs(new_quantity)
        elif self.quantity * new_quantity <= 0:
            self.entry = price if new_quantity else Decimal('0')
        self.quantity = new_quantity
        self.cash -= fee
        row.update(executedQty=str(filled), cumQuote=str(quote),
                   status='FILLED' if filled == Decimal(row['origQty']) else 'PARTIALLY_FILLED', updateTime=row['updateTime'] + 1)
        trade_id = len(self.trades) + 1
        self.trades.append(dict(symbol='NVDAUSDT', id=trade_id, orderId=row['orderId'], qty=str(qty),
                                quoteQty=str(qty * price), commission=str(fee), commissionAsset='USDT'))
        return {'e': 'ORDER_TRADE_UPDATE', 'E': row['updateTime'], 'T': row['updateTime'],
                'o': {'s': row['symbol'], 'c': key, 'S': row['side'], 'o': row['type'], 'ps': row['positionSide'],
                      'q': row['origQty'], 'i': row['orderId'], 'x': 'TRADE', 'X': row['status'],
                      'l': str(qty), 'z': str(filled), 'L': str(price), 'N': 'USDT', 'n': str(fee),
                      't': trade_id, 'T': row['updateTime']}}

    def funding_update(self, amount, *, symbol='NVDAUSDT', asset='USDT', time_ms=None,
                       tran_id=None, income_type='FUNDING_FEE', info='FUNDING_FEE',
                       apply_cash=True):
        if self.income is None:
            self.income = []
        signed = Decimal(str(amount))
        if tran_id is None:
            tran_id = len(self.income) + 1
        row = dict(
            symbol=symbol,
            incomeType=income_type,
            income=str(signed),
            asset=asset,
            time=MS if time_ms is None else time_ms,
            tranId=tran_id,
            tradeId='',
            info=info,
        )
        self.income.append(row)
        if apply_cash and asset == 'USDT':
            self.cash += signed
        return dict(row)
