"""Cash-funded equal-weight stock/ETF example for the shared V2 package loader."""
from decimal import Decimal

from factorlab.strategies.protocol import DecisionV2, StrategySpecV2


class BalancedPortfolio:
    def __init__(self, account_id, symbols=('SPY', 'IWM'), capital='100', interval_seconds=None):
        self.account_id, self.symbols = account_id, list(symbols)
        self.capital, self.interval_seconds = Decimal(capital), interval_seconds

    def describe(self):
        schedule = (dict(kind='calendar', calendar_id='alpaca', timezone='America/New_York',
                         event='session_open') if self.interval_seconds is None else
                    dict(kind='interval', interval_seconds=self.interval_seconds))
        return StrategySpecV2(strategy_id='alpaca-balanced', version='1',
            market='ALPACA_US_EQUITY', account_id=self.account_id, symbols=self.symbols,
            decision_kind='target_weights',
            data=dict(data_types=['closed_bar'], history_limit=1, historical_observations=False),
            schedule=schedule, state=dict(version='1', migration='preserve', from_versions=[]),
            capital=dict(currency='USD', requested=self.capital),
            execution=dict(order_types=['LIMIT'], time_in_force=['DAY'], shorting=False,
                           fractional_quantities=True, reduce_only=False))

    def decide(self, event, state):
        weight = Decimal('.9') / len(self.symbols)
        return DecisionV2('target_weights', targets=dict.fromkeys(self.symbols, weight),
                          state={'events': state.get('events', 0) + 1})
