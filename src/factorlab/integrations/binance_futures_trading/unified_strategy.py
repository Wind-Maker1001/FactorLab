"""Select an execution adapter from declared capabilities, not strategy names."""
from factorlab.strategies.decision_runtime import DecisionJournal, SignalAdapter, OrderStrategyRuntime
from .demo_market_feed import SOURCE, SYMBOL, INTERVALS
from .strategy_runtime import DemoStrategyRuntime


def strategy_runtime(controller, *, strategy=None, interval_seconds, deployment_id,
                     client_id_prefix, expect=None, max_notional=None, max_quantity=None, preview=False):
    if strategy is None:
        # Compatibility route for existing DualMA deployments and journals.
        from factorlab.trading.strategy import DualMAStrategy
        return DemoStrategyRuntime(controller, DualMAStrategy(source=SOURCE,
            interval_seconds=interval_seconds), client_id_prefix=client_id_prefix,
            expect=expect, max_notional=max_notional, max_quantity=max_quantity, preview=preview)
    session = controller if hasattr(controller, 'controller') else None
    account_controller = session.controller if session else controller
    if interval_seconds not in INTERVALS.values():
        raise ValueError('interval unavailable from current Demo feed')
    if session and not preview:
        # Exact campaign prefixes remain owned by the execution process.
        if not (deployment_id + '-').startswith(client_id_prefix):
            raise ValueError('deployment is outside the authorized campaign')
    journal = DecisionJournal(strategy, account_controller, deployment_id=deployment_id, symbol=SYMBOL,
        interval_seconds=interval_seconds, data_types=(['closed_bar'] if strategy.spec.decision_kind == 'target'
            else ['closed_bar', 'quote', 'order_event']))
    if strategy.spec.decision_kind == 'target':
        return DemoStrategyRuntime(controller, SignalAdapter(journal), client_id_prefix=client_id_prefix,
            expect=expect, max_notional=max_notional, max_quantity=max_quantity, preview=preview)
    return OrderStrategyRuntime(journal, session=session, expect=expect, preview=preview,
        max_notional=max_notional, max_quantity=max_quantity, client_id_prefix=client_id_prefix)
