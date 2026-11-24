"""Deterministic bounded S01 order strategy for software conformance validation.

NO LIVE MONEY / SOFTWARE_CONFORMANCE_ONLY:
This strategy exercises BUY LIMIT entry, confirmed fill/inventory-driven reduce-only
SELL LIMIT exit or deterministic cancellation, and optional fault injection through
the FACTORLAB_DECISION_V1 protocol. It is never a profitability or Champion claim.
"""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

from factorlab.strategies.protocol import Decision, StrategySpec
from factorlab.trading.contracts import OrderIntent, OrderStatus, OrderType, Side, TERMINAL


class LimitCycle:
    def __init__(self, symbol='NVDAUSDT', interval_seconds=60, quantity='.04',
                 tick='.01', buy_offset='.20', sell_offset='-.20',
                 mode='buy_then_sell', version='1.0.0', strategy_id='limit_cycle',
                 cancel_after_quotes=2, max_entry_price=None, max_exit_price=None,
                 exit_budget_notional=None):
        self.symbol = symbol
        self.interval = int(interval_seconds)
        self.quantity = D(str(quantity))
        self.tick = D(str(tick))
        self.buy_offset = D(str(buy_offset))
        self.sell_offset = D(str(sell_offset))
        self.mode = mode
        self.version = str(version)
        self.strategy_id = str(strategy_id)
        self.cancel_after_quotes = int(cancel_after_quotes)
        self.max_entry_price = None if max_entry_price is None else D(str(max_entry_price))
        self.max_exit_price = None if max_exit_price is None else D(str(max_exit_price))
        self.exit_budget_notional = None if exit_budget_notional is None else D(str(exit_budget_notional))

    def describe(self):
        return StrategySpec(self.strategy_id, self.version, 'orders',
                            [self.symbol], [self.interval], ['quote', 'order_event'])

    def _round_price(self, price, side):
        rounding = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
        return (price / self.tick).to_integral_value(rounding=rounding) * self.tick

    def decide(self, event, state):
        state = dict(state)
        if self.mode == 'fault_on_quote' and event['kind'] == 'quote':
            raise RuntimeError('deliberate S01 conformance fault')
        if self.mode == 'hold':
            return Decision(state=state)
        entry_id = event['deployment_id'] + '-entry'
        exit_id = event['deployment_id'] + '-exit'
        if event['kind'] == 'order_event':
            report = event['order_event']
            if report.client_order_id == entry_id:
                state['entry_status'] = report.status.value
                state['entry_filled'] = str(report.cumulative_filled_quantity)
                if report.status is OrderStatus.FILLED:
                    state['entry_done'] = True
                elif report.status in TERMINAL:
                    state['entry_closed'] = True
            elif report.client_order_id == exit_id:
                state['exit_status'] = report.status.value
                if report.status in TERMINAL:
                    state['exit_done'] = report.status is OrderStatus.FILLED
                    if report.status is not OrderStatus.FILLED:
                        state['recovery_required'] = 'EXIT_TERMINATED_WITH_POSSIBLE_INVENTORY'
            return Decision(state=state)

        # Quote event
        market = event['account']['market']
        inventory = event['account']['inventory']
        price = event['quote'].price

        if not state.get('entry_issued'):
            buy_price = self._round_price(price + self.buy_offset, Side.BUY)
            if self.max_entry_price is not None and buy_price > self.max_entry_price:
                state['entry_deferred'] = 'ENTRY_PRICE_EXCEEDS_RESERVED_BUDGET'
                return Decision(state=state)
            intent = OrderIntent(market, self.symbol, Side.BUY, OrderType.LIMIT,
                                 self.quantity, buy_price, entry_id, position_side='BOTH')
            state['entry_issued'] = True
            state['quotes_since_entry'] = 0
            return Decision(orders=(intent,), state=state)

        if self.mode in {'buy_then_sell', 'buy_then_cancel'}:
            if (state.get('entry_done') or state.get('entry_closed')) and inventory > 0 and not state.get('exit_issued'):
                sell_price = self._round_price(price + self.sell_offset, Side.SELL)
                if self.max_exit_price is not None:
                    sell_price = min(sell_price, self._round_price(self.max_exit_price, Side.SELL))
                intent = OrderIntent(market, self.symbol, Side.SELL, OrderType.LIMIT,
                                     inventory, sell_price, exit_id,
                                     position_side='BOTH', reduce_only=True)
                state['exit_issued'] = True
                state['recovery_required'] = 'EXIT_PENDING'
                return Decision(orders=(intent,), state=state)

        if self.mode in {'buy_then_sell', 'buy_then_cancel'}:
            if not state.get('entry_done') and not state.get('entry_closed'):
                quotes = state.get('quotes_since_entry', 0) + 1
                state['quotes_since_entry'] = quotes
                if quotes >= self.cancel_after_quotes and not state.get('cancel_issued'):
                    state['cancel_issued'] = True
                    return Decision(cancellations=(entry_id,), state=state)
            return Decision(state=state)

        return Decision(state=state)
