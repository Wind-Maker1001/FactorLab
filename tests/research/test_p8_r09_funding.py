from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.experiments.grid_f11_f08_offline import (
    DemoGuardrails,
    FundingSchedule,
    execution_profile,
    run_offline_lifecycle,
)


START = datetime(2026, 8, 13, tzinfo=timezone.utc)
BASELINE = DemoGuardrails(enable_f11=False, enable_f08=False)


def _bars(minutes: list[int], prices: list[float]) -> list[dict]:
    return [
        {"time": (START + timedelta(minutes=minute)).isoformat(), "close": price}
        for minute, price in zip(minutes, prices, strict=True)
    ]


@pytest.mark.parametrize(("interval", "minutes"), [("1m", 1), ("1h", 60), ("4h", 240)])
def test_constant_hourly_funding_matches_over_four_held_hours(interval, minutes):
    held_bars = 240 // minutes
    bars = _bars(
        [index * minutes for index in range(held_bars + 2)],
        [132.89] + [131.40] * (held_bars + 1),
    )
    result = run_offline_lifecycle(
        bars,
        interval=interval,
        guardrails=BASELINE,
        funding=FundingSchedule("constant_hourly_bps", 10),
    )

    assert result["holding_hours"] == pytest.approx(4)
    assert result["residual_inventory"] == pytest.approx(0.04)
    assert result["funding_events_count"] == held_bars
    assert result["realized_funding_usdt"] == pytest.approx(-0.04 * 131.40 * 0.001 * 4, abs=1e-8)
    assert result["reconciled_zero_residual"]


def test_constant_funding_uses_elapsed_minutes_and_partial_inventory_across_windows():
    bars = _bars(
        [0, 1, 2, 3, 4, 6, 7],
        [132.89, 131.56, 131.56, 131.00, 130.00, 129.00, 131.20],
    )
    result = run_offline_lifecycle(
        bars,
        interval="1m",
        guardrails=DemoGuardrails(window_orders=1, window_seconds=60),
        assumptions=replace(execution_profile("LIMITED_CAPACITY_PARTIAL"), slippage=D(0)),
        funding=FundingSchedule("constant_hourly_bps", 10),
        record_hourly_series=True,
    )

    assert [bar["inventory"] for bar in result["hourly_series"]] == [0, 0.02, 0.04, 0.02, 0.02, 0.02, 0]
    assert result["holding_hours"] == pytest.approx(0.1)
    assert result["exit_orders_partial_events"] == 1
    assert result["funding_events_count"] == 5
    notional_minutes = 0.02 * (131.56 + 130.00 + 2 * 129.00 + 131.20) + 0.04 * 131.00
    assert result["realized_funding_usdt"] == pytest.approx(-notional_minutes * 0.001 / 60, abs=2.5e-8)
    assert result["reconciled_zero_residual"]


@pytest.mark.parametrize(
    ("mode", "hourly_bps", "event_count", "amount"),
    [("historical_events", 10, 1, -0.04 * 131.40 * 0.003), ("constant_hourly_bps", 0, 0, 0)],
)
def test_funding_modes_book_only_the_selected_schedule(mode, hourly_bps, event_count, amount):
    event = (START + timedelta(minutes=2), D("131.40"), D("0.003"), "test")
    result = run_offline_lifecycle(
        _bars([0, 1, 2], [132.89, 131.40, 131.40]),
        interval="1m",
        guardrails=BASELINE,
        funding=FundingSchedule(mode, hourly_bps, events=(event,)),
    )

    assert result["funding_events_count"] == event_count
    assert result["realized_funding_usdt"] == pytest.approx(amount, abs=1e-8)
    assert result["reconciled_zero_residual"]
