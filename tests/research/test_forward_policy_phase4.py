"""Market-local clock guards for genuine forward publications."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from factorlab.forward.policy import (
    ForwardTimingError,
    classify_forward_timing,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_current_signal_is_on_time_only_after_market_close():
    timing = classify_forward_timing(
        "2026-09-24",
        activation_date="2026-09-24",
        generated_at=datetime(2026, 9, 24, 15, 30, tzinfo=SHANGHAI),
    )

    assert timing.timeliness == "ON_TIME"
    assert timing.generated_at.isoformat().endswith("+08:00")
    assert timing.reissue_reason is None

    with pytest.raises(ForwardTimingError, match="before market close"):
        classify_forward_timing(
            "2026-09-24",
            activation_date="2026-09-24",
            generated_at=datetime(2026, 9, 24, 15, 29, tzinfo=SHANGHAI),
        )


def test_old_signal_is_always_an_explained_late_reissue():
    timing = classify_forward_timing(
        "2026-09-24",
        activation_date="2026-09-24",
        generated_at=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc),
        reissue_reason="approved upstream correction",
    )

    assert timing.timeliness == "LATE_REISSUE"
    assert timing.generated_at.date().isoformat() == "2026-09-25"

    with pytest.raises(ForwardTimingError, match="requires --reissue-reason"):
        classify_forward_timing(
            "2026-09-24",
            activation_date="2026-09-24",
            generated_at=datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc),
        )


def test_future_and_pre_activation_dates_are_rejected():
    now = datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI)
    with pytest.raises(ForwardTimingError, match="future-dated"):
        classify_forward_timing(
            "2026-09-25",
            activation_date="2026-09-24",
            generated_at=now,
        )
    with pytest.raises(ForwardTimingError, match="predates"):
        classify_forward_timing(
            "2026-09-23",
            activation_date="2026-09-24",
            generated_at=now,
            reissue_reason="not permitted",
        )


def test_utc_clock_is_converted_before_same_day_classification():
    with pytest.raises(ForwardTimingError, match="requires --reissue-reason"):
        classify_forward_timing(
            "2026-09-24",
            activation_date="2026-09-24",
            # 16:10 UTC is already 00:10 on 25 September in Shanghai.
            generated_at=datetime(2026, 9, 24, 16, 10, tzinfo=timezone.utc),
        )
