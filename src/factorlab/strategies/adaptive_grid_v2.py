"""Native adaptive grids driven by V2 observations and authoritative owner projections."""
from datetime import timedelta
from decimal import Decimal
import json

from factorlab.trading import Market, OrderEvent, OrderStatus
from factorlab.trading.realtime import identity
from factorlab.trading.storage import decode, dumps
from .grid_execution import GridAccount
from .grid_restore import restore_grid
from .protocol import DecisionV2, StrategySpecV2


class AdaptiveGridV2:
    def __init__(self, deployment, spec):
        self.deployment = dict(deployment)
        spec = dict(spec, capital=dict(spec['capital'], requested=Decimal(spec['capital']['requested'])))
        self.spec = StrategySpecV2(**spec)
        kind = deployment['r10_strategy']
        if (kind not in {'B_FUNDING_SENSITIVE_GRID', 'C_VOL_ADAPTIVE_GRID'}
                or self.spec.market != Market.BINANCE_USDM.value
                or self.spec.decision_kind != 'orders' or len(self.spec.symbols) != 1
                or self.spec.schedule != dict(kind='interval', interval_seconds=3600)
                or not {'closed_bar', 'order_event'} <= set(self.spec.data['data_types'])):
            raise ValueError('adaptive grid requires an hourly single-symbol USD-M orders declaration')
        if kind == 'B_FUNDING_SENSITIVE_GRID' and 'funding_settlement' not in self.spec.data['data_types']:
            raise ValueError('funding grid requires settled funding observations')
        if kind == 'C_VOL_ADAPTIVE_GRID' and len(deployment['warmup_closes']) < deployment['vol_policy']['vol_window_hours'] + 1:
            raise ValueError('volatility grid requires its frozen warmup window')

    def describe(self):
        return self.spec

    def migrate_state(self, state, from_version):
        return state

    def _engine(self, event):
        values = dict(self.deployment)
        values.update(deployment_id='dg' + identity((event['binding']['instance_id'],
            event['context']['package_id']))[:30], account_id=event['binding']['account_id'],
            instrument_id=self.spec.symbols[0], market=Market.BINANCE_USDM,
            initial_equity=Decimal(event['allocation']['capital']))
        values['rules'] = {key: Decimal(value) for key, value in values['rules'].items()}
        values['rules'].update(event['context']['grid_rules'][self.spec.symbols[0]])
        if values['r10_strategy'] == 'B_FUNDING_SENSITIVE_GRID':
            values['funding_events'] = []
        return restore_grid(values)

    def decide(self, event, state):
        symbol = self.spec.symbols[0]
        context = event['context']
        binding = identity((event['binding'], context['package_id'], self.deployment))
        grid = self._engine(event)
        if state:
            if state['binding'] != binding:
                raise ValueError('adaptive grid checkpoint binding changed')
            grid.restore_decision_checkpoint(decode(json.loads(state['grid'])))
        account = GridAccount(**context['grid_accounts'][symbol])
        execution = context['execution']
        native_to_client = execution['native_to_client']
        funding = dict(state.get('funding', {}))
        if self.deployment['r10_strategy'] == 'B_FUNDING_SENSITIVE_GRID':
            for observation in event['symbols'][symbol]['history'].get('funding_settlement', []):
                payload = observation['payload']
                key = payload['event_id']
                value = dumps((observation['effective_at'], payload['mark_price'],
                               payload['funding_rate'], observation['source']))
                if key in funding and funding[key] != value:
                    raise ValueError('settled funding identity changed')
                funding[key] = value
            events = sorted((tuple(decode(json.loads(value))) for value in funding.values()), key=lambda value: value[0])
            grid._all_funding_events = events
            grid._all_funding_times = [value[0] for value in events]
            if event['data_type'] == 'closed_bar':
                start = event['event_time'] - timedelta(hours=grid.funding_policy.lookback_hours)
                if not events or events[0][0] > start or events[-1][0] <= start:
                    return DecisionV2('orders', state=dict(binding=binding,
                        grid=dumps(grid.decision_checkpoint()), funding=funding))
        if event['data_type'] == 'order_event':
            payload = dict(event['symbols'][symbol]['observations']['order_event']['payload'])
            client_to_native = {client: native for native, client in native_to_client.items()}
            payload['client_order_id'] = client_to_native[payload['client_order_id']]
            grid.confirm(OrderEvent(**payload), account, delivery=grid.delivery + 1)
        elif event['data_type'] == 'closed_bar':
            bar = event['symbols'][symbol]['observations']['closed_bar']['payload']
            value = dict(id=identity(event['event_id']), time=bar['closed_at'].replace(tzinfo=None).isoformat(),
                price=bar['close'], close=bar['close'], high=bar['high'], low=bar['low'], volume=0,
                completed_bar=True, source_bar=bar)
            grid.observe(value, account, delivery=grid.delivery + 1)
            if grid.pending_bar is not None:
                grid.complete_bar(delivery=grid.delivery + 1)
        else:
            raise ValueError('adaptive grid requires a bar or original order confirmation')
        submitted = set(native_to_client)
        withdrawn = [order.intent.client_order_id for order in grid.active
            if order.status is OrderStatus.NEW and order.intent.client_order_id in grid.cancellations
            and order.intent.client_order_id not in submitted]
        if withdrawn:
            grid.withdraw_unsubmitted(withdrawn, account, delivery=grid.delivery + 1)
        orders = tuple(order.intent for order in grid.active
            if order.status is OrderStatus.NEW and order.intent.client_order_id not in submitted
            and order.intent.client_order_id not in grid.cancellations)
        cancellations = tuple(dict(symbol=symbol, client_order_id=native_to_client[key])
            for key in sorted(grid.cancellations) if key in native_to_client)
        if funding:
            start = event['event_time'] - timedelta(hours=grid.funding_policy.lookback_hours)
            if grid.entry_time_utc is not None:
                start = min(start, grid.entry_time_utc)
            older = [(key, decode(json.loads(value))[0]) for key, value in funding.items()
                     if decode(json.loads(value))[0] < start]
            keep = max(older, key=lambda value: value[1])[0] if older else None
            funding = {key: value for key, value in funding.items()
                       if key == keep or decode(json.loads(value))[0] >= start}
        return DecisionV2('orders', orders=orders, cancellations=cancellations,
            state=dict(binding=binding, grid=dumps(grid.decision_checkpoint()), funding=funding))
