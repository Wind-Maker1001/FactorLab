"""Local paper assumptions, never exchange account eligibility or live authority."""
from dataclasses import dataclass
from factorlab.integrations.binance_futures_trading.rules import RuleObservation


@dataclass(frozen=True)
class PaperSimulationPolicy:
    """Fixed 1,000-unit accounts with full notional collateral, not liquidation models."""

    def describe(self):
        return dict(mode='PAPER_SIMULATION', simulated=True, initial_cash='1000',
            collateral_ratio='1', leverage='SYNTHETIC_1X_NO_LIQUIDATION_MODEL',
            account_permission='ASSUMED_PAPER_ONLY', real_account_permission='UNKNOWN',
            account_risk_brackets='UNKNOWN', payment_eligibility='UNKNOWN', settlement='UNKNOWN',
            custody='NOT_SIMULATED', stock_reference='QUOTE_IF_PUBLIC_REFERENCE_UNAVAILABLE',
            liquidity='SYNTHETIC_QUOTE_PARTICIPATION', live_execution='BLOCKED')

    def futures_rules(self, public_symbol, received, source):
        # This tier is a local paper capital ceiling, not a reported account bracket.
        return RuleObservation.from_exchange(public_symbol,
            dict(symbol=public_symbol['symbol'], brackets=[dict(notionalFloor='0',
                notionalCap='1000', initialLeverage=1)]), received, 'SYNTHETIC_PAPER_COLLATERAL:' + source)


PAPER_SIMULATION = PaperSimulationPolicy()
