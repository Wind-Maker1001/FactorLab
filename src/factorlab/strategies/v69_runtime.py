"""Native V69 package entrypoint over the unified multi-instrument boundary."""
from decimal import Decimal as D

import pandas as pd

from factorlab.trading.contracts import Market
from .protocol import DecisionV2, StrategySpecV2
from .v69 import ASSETS, V69Strategy
from .v69_alpaca import original_plan_quantities, to_adjusted_holdings


class V69AlpacaStrategy:
    def __init__(self, account_id, capital='0'):
        self.account_id, self.capital = account_id, D(capital)

    def describe(self):
        return StrategySpecV2(strategy_id='V69_CAUSAL', version='69.2',
            market=Market.ALPACA_US_EQUITY.value, account_id=self.account_id,
            symbols=list(ASSETS), decision_kind='quantity_deltas',
            data=dict(data_types=['closed_bar', 'quote'], history_limit=252, historical_observations=False),
            schedule=dict(kind='calendar', calendar_id='alpaca', timezone='America/New_York',
                          event='natural_week_open'),
            state=dict(version='v69-native-1', migration='preserve', from_versions=[]),
            capital=dict(currency='USD', requested=self.capital),
            execution=dict(order_types=['LIMIT'], time_in_force=['DAY'], shorting=False,
                           fractional_quantities=True, reduce_only=False))

    def decide(self, event, state):
        from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
        context = event['context']['native_v69']
        if 'frozen_original' in context:
            decision = context['frozen_original']
            targets = ({} if decision['weights'] is None else original_plan_quantities(decision,
                raw_holdings=context['raw_holdings'], factors=context['factors'],
                quantity_steps=context['quantity_steps'])[1])
            return DecisionV2('quantity_deltas', targets=targets,
                              state=dict(checkpoint=decision['state'], original=decision))
        calendar = AlpacaSessionCalendar(context['calendar'],
            coverage_start=context['coverage_start'], coverage_end=context['coverage_end'])
        history = {symbol: [row['payload'] for row in event['symbols'][symbol]['history']['closed_bar']]
                   for symbol in ASSETS}
        close = pd.DataFrame({symbol: pd.Series(
            {pd.Timestamp(row['session']): float(row['adjusted_close']) for row in rows})
            for symbol, rows in history.items()}).reindex(columns=ASSETS).sort_index()
        volume = pd.DataFrame({symbol: pd.Series(
            {pd.Timestamp(row['session']): float(row['adjusted_volume']) for row in rows})
            for symbol, rows in history.items()}).reindex(columns=ASSETS).sort_index()
        if close.shape != (252, 8) or volume.shape != (252, 8) or close.isna().any().any() or volume.isna().any().any():
            raise ValueError('original V69 requires complete 252-session eight-asset matrices')
        holdings = {s: D(context['raw_holdings'].get(s, '0')) for s in ASSETS}
        factors = {s: D(context['factors'][s]) for s in ASSETS}
        steps = {s: D(context['quantity_steps'][s]) for s in ASSETS}
        checkpoint = state.get('checkpoint', state)
        decision = V69Strategy().decide(dict(session=context['session'], calendar=calendar,
            close=close, volume=volume, holdings=to_adjusted_holdings(holdings, factors),
            cash=float(event['allocation']['cash'])), checkpoint)
        if decision['weights'] is None:
            return DecisionV2('quantity_deltas', state=dict(checkpoint=decision['state'], original=decision))
        _, deltas = original_plan_quantities(decision, raw_holdings=holdings, factors=factors, quantity_steps=steps)
        return DecisionV2('quantity_deltas', targets=deltas,
                          state=dict(checkpoint=decision['state'], original=decision))
