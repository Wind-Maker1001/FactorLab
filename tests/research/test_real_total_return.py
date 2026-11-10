"""Artificial holdings test bounded rights, never historical evidence."""
import pandas as pd
import pytest

from factorlab.errors import SchemaError
from factorlab.strategies.real_total_return import candidate_entitlements


def event(key="test", record="2018-06-20", ex="2018-06-21"):
    return dict(event_id=key, event_type="CASH_DIVIDEND", record_date=record,
                ex_date=ex, scheduled_pay_date="2019-01-02", cash_per_share="0.5")


def path(shares):
    return pd.DataFrame(dict(date=pd.date_range("2018-06-19", periods=4),
                             symbol="601857.SH", shares=shares))


@pytest.mark.parametrize("shares,expected", [([100,100,0,0], "50.0"),
    ([0,0,100,100], "0.0"), ([0,200,200,200], "100.0")])
def test_record_close_not_ex_or_terminal(shares, expected):
    row = candidate_entitlements(path(shares), [event()])[0]
    assert row["gross_entitlement"] == expected
    assert row["actual_received"] is None
    assert row["tax"] is None


def test_multiple_events_and_unpaid_end_do_not_authorize_receipt():
    rows = candidate_entitlements(path([100,200,300,0]),
        [event(), event("second", "2018-06-21", "2018-06-22")])
    assert [r["gross_entitlement"] for r in rows] == ["100.0", "150.0"]
    assert all(r["actual_received"] is None for r in rows)


def test_duplicate_revision_rejected():
    with pytest.raises(SchemaError, match="duplicate/revised"):
        candidate_entitlements(path([100]*4), [event(), event()])


def test_missing_record_observation_rejected():
    with pytest.raises(SchemaError, match="record/ex"):
        candidate_entitlements(path([100]*4), [event(record="2018-06-18")])


def test_share_change_not_silently_ignored():
    item = event()
    item["event_type"] = "BONUS"
    with pytest.raises(SchemaError, match="share-change"):
        candidate_entitlements(path([100]*4), [item])
