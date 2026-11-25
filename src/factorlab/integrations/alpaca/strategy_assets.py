"""Official US equity identity and cash-only strategy qualification."""
from decimal import Decimal as D

from factorlab.trading.contracts import Market
from factorlab.trading.portfolio_execution import InstrumentRules
from .market_data.inputs import Instrument


class StrategyAssets:
    def __init__(self, adapter, store, *, clock):
        self.adapter, self.store, self.clock = adapter, store, clock
        self.assets = {}

    def qualify(self, spec, binding, allocation):
        spec.require_binding(binding)
        if spec.market != Market.ALPACA_US_EQUITY.value or spec.capital['currency'] != 'USD':
            raise ValueError('Alpaca US equity USD binding required')
        if (spec.execution['shorting'] or spec.execution['reduce_only']
                or not spec.execution['order_types']
                or not set(spec.execution['order_types']) <= {'LIMIT', 'MARKET'}
                or not spec.execution['time_in_force']
                or not set(spec.execution['time_in_force']) <= {'DAY'}):
            raise ValueError('unsupported cash-only Alpaca execution requirements')
        if spec.capital['requested'] > allocation['capital']:
            raise ValueError('strategy requested capital exceeds its allocation')
        if not set(spec.data['data_types']) <= {'closed_bar', 'quote', 'order_event'}:
            raise ValueError('unsupported Alpaca data requirements')
        schedule = spec.schedule
        if schedule['kind'] == 'calendar':
            if (schedule['calendar_id'] != 'alpaca' or schedule['timezone'] != 'America/New_York'
                    or schedule['event'] not in {'session_open', 'natural_week_open'}):
                raise ValueError('unsupported Alpaca calendar schedule')
        elif (schedule['kind'] != 'interval' or type(schedule['interval_seconds']) is not int
              or not 60 <= schedule['interval_seconds'] <= 3600 or schedule['interval_seconds'] % 60):
            raise ValueError('unsupported Alpaca bar interval')
        for symbol in spec.symbols:
            self.resolve(symbol, fractional=spec.execution['fractional_quantities'])

    def resolve(self, symbol, *, fractional=False):
        asset = self.adapter.read_asset(symbol)
        if (asset['symbol'] != symbol or asset['class'] != 'us_equity'
                or asset['status'] != 'active' or not asset['tradable'] or not asset['id']):
            raise ValueError('official asset is not an active tradable US equity: ' + symbol)
        if fractional and not asset['fractionable']:
            raise ValueError('asset does not support requested fractional quantities: ' + symbol)
        key = 'alpaca-asset:' + self.adapter.read_account_details().account_id + ':' + symbol
        self.store.record_metadata(key, dict(asset_id=asset['id'], symbol=symbol, asset_class=asset['class']))
        self.assets[symbol] = asset
        return asset

    def instruments(self, symbols, *, at=None):
        at = self.clock() if at is None else at
        return tuple(Instrument(self.assets[symbol]['id'], symbol, at) for symbol in symbols)

    def rules(self, symbols, *, prices=None, legacy_v69=False):
        rules = {}
        for symbol in symbols:
            asset = self.resolve(symbol)
            tick = D('.01') if legacy_v69 or prices is None or prices[symbol] >= 1 else D('.0001')
            rules[symbol] = InstrumentRules(D('.000000001') if asset['fractionable'] else D(1),
                D(1), tradable=asset['tradable'], time_in_force='DAY', price_tick=tick,
                limit_price_tolerance=D('.001'))
        return rules
