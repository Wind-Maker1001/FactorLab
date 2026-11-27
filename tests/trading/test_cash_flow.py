from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from factorlab.trading.cash_flow import cash_flow_checkpoint
from factorlab.trading.storage import decode, dumps
import json


START = datetime(2026, 11, 24, tzinfo=timezone.utc)


def checkpoint(previous, records, start, end):
    return cash_flow_checkpoint(previous, records, start=START + timedelta(hours=start),
                                end=START + timedelta(hours=end),
                                retain_from=START + timedelta(hours=end - 1))


def test_overlapping_cash_history_restarts_without_double_counting():
    deposit = ('deposit', START + timedelta(minutes=30), D(100))
    withdrawal = ('withdrawal', START + timedelta(hours=1, minutes=30), D(-40))
    first = checkpoint(None, [deposit, deposit], 0, 1)
    restored = decode(json.loads(dumps(first)))
    second = checkpoint(restored, [deposit, withdrawal], 0, 2)
    assert second['total'] == D(60)
    assert second['origin'] == START
    assert second['tail'] == {'withdrawal': withdrawal[1:]}
    third = checkpoint(second, [withdrawal], 1, 3)
    assert third['total'] == D(60)
    assert third['tail'] == {}
    assert checkpoint(third, [], 2, 4)['total'] == D(60)


def test_reread_overlap_replaces_source_corrections():
    first = checkpoint(None, [('transfer', START, D(100))], 0, 1)
    corrected = checkpoint(first, [('transfer', START, D(90))], 0, 2)
    assert corrected['total'] == D(90)


def test_conflicting_identity_in_one_observation_is_rejected():
    with pytest.raises(ValueError, match='identity changed'):
        checkpoint(None, [('transfer', START, D(100)), ('transfer', START, D(90))], 0, 1)
