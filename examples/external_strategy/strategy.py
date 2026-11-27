"""Minimal V2 entrypoints for a read-only benchmark and frozen linear inference."""
from decimal import Decimal

from factorlab.strategies.protocol import DecisionV2, StrategySpecV2


class ReferenceTrend:
    def __init__(self, account_id, reference_source):
        self.account_id = account_id
        self.reference_source = reference_source

    def describe(self):
        return StrategySpecV2(
            strategy_id='reference-trend', version='1', market='ALPACA_US_EQUITY',
            account_id=self.account_id, symbols=['SPY'], decision_kind='target_weights',
            data=dict(data_types=['closed_bar'], history_limit=1, historical_observations=False,
                      account_fields=[], allocation_fields=[],
                      references={'IWM': {'closed_bar': dict(source=self.reference_source, price_basis='raw',
                                                           interval_seconds=86400, history_limit=2)}}),
            schedule=dict(kind='calendar', calendar_id='alpaca', timezone='America/New_York',
                          event='session_open'),
            state=dict(version='1', migration='preserve', from_versions=[]),
            capital=dict(currency='USD', requested=Decimal(100)),
            execution=dict(order_types=['LIMIT'], time_in_force=['DAY'], shorting=False,
                           fractional_quantities=True, reduce_only=False))

    def decide(self, event, state):
        previous, latest = event['references']['IWM']['history']['closed_bar']
        rising = latest['payload']['close'] > previous['payload']['close']
        return DecisionV2('target_weights', targets={'SPY': Decimal('.9') if rising else Decimal(0)},
                          state={'events': state.get('events', 0) + 1})


class FrozenReturn:
    def __init__(self, account_id, version, model):
        self.account_id = account_id
        self.version = version
        self.model = model

    def describe(self):
        return frozen_return_spec(self.account_id, self.version)

    def decide(self, event, state):
        previous, latest = event['symbols']['NVDAUSDT']['history']['closed_bar']
        change = latest['payload']['close'] / previous['payload']['close'] - 1
        score = self.model.predict({'return_1': change}, event_time=event['event_time'])
        return DecisionV2('target_weights', targets={'NVDAUSDT': Decimal('.5') if score > 0 else Decimal(0)},
                          state={'events': state.get('events', 0) + 1})


def frozen_return_spec(account_id, version):
    return StrategySpecV2(
        strategy_id='frozen-return', version=version, market='BINANCE_USDM', account_id=account_id,
        symbols=['NVDAUSDT'], decision_kind='target_weights',
        data=dict(data_types=['closed_bar'], history_limit=2, historical_observations=False,
                  account_fields=[], allocation_fields=[]),
        schedule=dict(kind='interval', interval_seconds=60),
        state=dict(version='1', migration='preserve', from_versions=[]),
        capital=dict(currency='USDT', requested=Decimal(7)),
        execution=dict(order_types=['LIMIT'], time_in_force=['GTC'], shorting=False,
                       fractional_quantities=True, reduce_only=False))
