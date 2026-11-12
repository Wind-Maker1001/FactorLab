"""Exchange-reported account facts; missing values stay None (UNKNOWN)."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from factorlab.errors import DataQualityError
from .quantitative import QuantitativeObservation
from factorlab.integrations.binance.market import _decimal
from factorlab.trading import AccountSnapshot, AssetBalance, Market, Position


def instant(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise DataQualityError('invalid exchange timestamp')
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


def number(row, key):
    return None if row.get(key) is None else _decimal(row[key])


@dataclass(frozen=True)
class AccountFacts:
    """Raw authenticated read results, not public funding forecasts."""
    account_id: str
    observed_at: datetime
    assets: tuple[dict, ...]
    positions: tuple[dict, ...]
    risk: tuple[dict, ...]
    open_orders: tuple[dict, ...]
    trades: tuple[dict, ...]
    income: tuple[dict, ...]
    hedge_mode: bool | None
    symbol_config: tuple[dict, ...] = ()
    source_complete: bool = False
    can_trade: bool | None = None
    quantitative: QuantitativeObservation | None = None

    def usdt(self):
        row = next((a for a in self.assets if a.get('asset') == 'USDT'), {})
        return {key: number(row, key) for key in (
            'walletBalance', 'availableBalance', 'marginBalance', 'unrealizedProfit',
            'initialMargin', 'maintMargin', 'positionInitialMargin', 'openOrderInitialMargin')}

    def position_details(self):
        rows = []
        for row in self.positions:
            risk = next((r for r in self.risk if (r.get('symbol'), r.get('positionSide')) ==
                         (row.get('symbol'), row.get('positionSide'))), {})
            config = next((c for c in self.symbol_config if c.get('symbol') == row.get('symbol')), {})
            rows.append({
                'symbol': row.get('symbol'), 'position_side': row.get('positionSide'),
                'quantity': number(row, 'positionAmt'), 'entry_price': number(row, 'entryPrice'),
                'unrealized_pnl': number(row, 'unrealizedProfit'),
                'initial_margin': number(row, 'initialMargin'), 'maintenance_margin': number(row, 'maintMargin'),
                'leverage': config.get('leverage'), 'margin_type': config.get('marginType'),
                'isolated_margin': number(risk, 'isolatedMargin'),
                'liquidation_price': number(risk, 'liquidationPrice'),
                'mark_price': number(risk, 'markPrice'), 'notional': number(risk, 'notional'),
            })
        return tuple(rows)

    def funding_payments(self):
        return tuple(row for row in self.income if row.get('incomeType') == 'FUNDING_FEE')

    def shared(self, orders=(), *, reconciled=False):
        """Only project complete one-way USDT fields; never replace UNKNOWN with 0."""
        values = self.usdt()
        total, available = values['walletBalance'], values['availableBalance']
        assets = () if total is None or available is None else (AssetBalance('USDT', total, total - available),)
        positions = []
        complete = self.source_complete and bool(assets) and self.hedge_mode is False
        for row in self.position_details():
            quantity = row['quantity']
            if quantity is None:
                complete = False
                continue
            if quantity == 0:
                continue
            if row['position_side'] != 'BOTH' or row['entry_price'] is None or row['mark_price'] is None:
                complete = False
                continue
            positions.append(Position(row['symbol'], quantity, row['entry_price'], row['mark_price'], 'USDT'))
        return AccountSnapshot(Market.BINANCE_USDM, self.account_id, assets, tuple(positions), tuple(orders),
                               self.observed_at, reconciled and complete)
