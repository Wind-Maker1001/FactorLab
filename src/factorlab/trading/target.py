"""Long-only collateralized target sizing shared by execution compositions."""
from decimal import Decimal


def target_quantity(*, equity, weight, price, budget_price, fee_rate, current, available, step):
    target = max(Decimal(0), equity) * weight / (price * (1 + fee_rate))
    if target > current:
        target = max(current, min(
            max(Decimal(0), equity) * weight / (budget_price * (1 + fee_rate)),
            current + available / (budget_price * (1 + fee_rate))))
    return (target // step) * step if step else target
