"""Account-bound Demo symbol discovery; discovery never grants an allocation."""
from dataclasses import dataclass
import json

from factorlab.integrations.binance.market import symbol_name
from factorlab.trading.storage import decode

from .execution import DemoRuleObservation, observe_demo_rules
from .protocol import SafetyBlocked


@dataclass(frozen=True)
class DemoSymbolContext:
    observation: DemoRuleObservation
    configuration: dict

    @property
    def symbol(self):
        return self.observation.rules.symbol

    @property
    def rules(self):
        return self.observation.rules

    @property
    def leverage(self):
        return self.configuration['leverage']


class DemoSymbolRegistry:
    """Persist observations in the owner's journal, reacquiring rules on restart."""

    def __init__(self, adapter, store):
        self.adapter = adapter
        self.store = store
        self.contexts = {}

    def observe(self, symbol):
        from .demo_session import demo_account_configuration
        from .rules import SUPPORTED_NETWORK_FILTERS

        symbol = symbol_name(symbol)

        def qualify(row):
            if (row.get('contractType') not in {'PERPETUAL', 'TRADIFI_PERPETUAL'}
                    or 'GTC' not in row.get('timeInForce', ())
                    or 'LIMIT' not in row.get('orderTypes', ())):
                raise SafetyBlocked('Demo symbol lacks perpetual GTC LIMIT support')
            filters = {f['filterType'] for f in row.get('filters', ())}
            if (filters - SUPPORTED_NETWORK_FILTERS
                    or not {'PRICE_FILTER', 'LOT_SIZE', 'MIN_NOTIONAL'} <= filters):
                raise SafetyBlocked('Demo symbol has unsupported or incomplete rules')

        observation = observe_demo_rules(self.adapter, symbol,
            source_version=self.adapter.client.environment.host, on_symbol=qualify)
        facts = self.adapter.read_facts()
        configuration = demo_account_configuration(facts, symbol)
        context = DemoSymbolContext(observation, configuration)
        rules = observation.rules
        self.store.record_metadata('demo:symbol:' + symbol + ':' + rules.observed_at.isoformat(),
            dict(symbol=symbol, account_id=observation.account_id,
                 source=rules.source_version, observed_at=rules.observed_at,
                 status='TRADING', filters=rules.filters, brackets=rules.brackets,
                 configuration=configuration))
        self.contexts[symbol] = context
        return context

    def require(self, symbol):
        context = self.contexts.get(symbol)
        if context is None or not context.rules.is_fresh(self.adapter.clock()):
            context = self.observe(symbol)
        return context

    def allocation(self, allocation_id):
        raw = self.store.event_payload('demo:allocation:' + allocation_id)
        if raw is None:
            raise SafetyBlocked('Demo allocation is not authorized')
        return decode(json.loads(raw))
