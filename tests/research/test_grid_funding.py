from datetime import timedelta
from decimal import Decimal as D
import pytest
from factorlab.experiments.grid_funding import (
    START, END, cashflow, fragment_funding, settlements, drawdown,
)


@pytest.mark.parametrize('quantity,rate,expected', [
    ('2', '.01', '-2'), ('-2', '.01', '2'),
    ('2', '-.01', '2'), ('-2', '-.01', '-2'), ('0', '.01', '0'),
])
def test_cashflow(quantity, rate, expected):
    assert cashflow(quantity, '100', rate) == D(expected)


def test_repeated_settlements_and_ties():
    events = [(START+timedelta(hours=h), D(100), D('.01'), 'Regular') for h in (1,2,3)]
    assert fragment_funding(D(2), START, END, events) == -6
    assert fragment_funding(D(2), events[0][0], events[-1][0], events) == -4
    assert fragment_funding(D(2), events[0][0], events[-1][0], events, fills_first=True) == -4
    assert fragment_funding(D(2), START, events[0][0], events, fills_first=True) == 0
    assert fragment_funding(D(2), START, events[0][0], events) == -2


def test_partial_quantity_and_independent_accounts():
    events = [(START+timedelta(hours=1), D(100), D('.01'), 'Regular')]
    a = fragment_funding(D('.1'), START, END, events)
    b = fragment_funding(D('.2'), START, END, events)
    assert a == D('-.1') and b == D('-.2')
    assert a+b == fragment_funding(D('.3'), START, END, events)


def test_window_and_missing_price():
    row = dict(symbol='NVDAUSDT', fundingTime=int(START.timestamp()*1000),
               fundingRate='.01', markPrice='100', rateType='Regular')
    assert settlements([row])[0][0] == START
    with pytest.raises(ValueError, match='outside'):
        settlements([{**row, 'fundingTime': int(END.timestamp()*1000)}])
    with pytest.raises(ValueError, match='mark price'):
        settlements([{**row, 'markPrice': None}])
    with pytest.raises(ValueError, match='duplicate'):
        settlements([row, row])


def test_overlay_drawdown():
    assert drawdown([D(1000), D(990), D(1010)]) == 1
    assert drawdown([D(1000), D(988), D(1008)]) == D('1.2')
