import json
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.strategies.v69 import ASSETS, V69Strategy
from factorlab.strategies.v69_alpaca import original_plan_quantities, to_adjusted_holdings
from factorlab.strategies.v69_runtime import V69AlpacaStrategy


@pytest.mark.parametrize('checkpoint', [{}, {'last_week': [2026, 48]}])
@pytest.mark.parametrize('factor', ['1', '.5'])
def test_native_entry_preserves_original_inputs_decision_and_exact_shares(checkpoint, factor):
    session = '2026-11-23'
    days = pd.bdate_range(end='2026-11-20', periods=252)
    rows = [dict(date=str(day.date()), open='09:30', close='16:00')
            for day in days.union(pd.DatetimeIndex([session]))]
    coverage_start = str((days[0] - pd.Timedelta(days=7)).date())
    calendar = AlpacaSessionCalendar(rows, coverage_start=coverage_start, coverage_end=session)
    rng = np.random.default_rng(69)
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(.0005, .018, (252, 8)), axis=0)),
                         index=days, columns=ASSETS)
    volume = pd.DataFrame(100000., index=days, columns=ASSETS)
    holdings = {symbol: D(index) for index, symbol in enumerate(ASSETS)}
    factors = dict.fromkeys(ASSETS, D(factor))
    steps = dict.fromkeys(ASSETS, D('.000000001'))
    original = V69Strategy().decide(dict(session=session, calendar=calendar, close=close,
        volume=volume, holdings=to_adjusted_holdings(holdings, factors), cash=100000.), checkpoint)
    event = dict(allocation={'cash': D(100000)}, context={'native_v69': dict(
        calendar=rows, coverage_start=coverage_start, coverage_end=session, session=session,
        raw_holdings=holdings, factors=factors, quantity_steps=steps)}, symbols={
        symbol: {'history': {'closed_bar': [dict(payload=dict(session=str(day.date()),
            adjusted_close=close.loc[day, symbol], adjusted_volume=volume.loc[day, symbol]))
            for day in days]}} for symbol in ASSETS})
    native = V69AlpacaStrategy('paper', '100000')
    decision = native.decide(event, {'checkpoint': checkpoint})
    assert decision.state['original'] == original
    assert json.loads(json.dumps(decision.state))['checkpoint'] == original['state']
    if original['weights'] is None:
        assert not decision.targets
    else:
        assert decision.targets == original_plan_quantities(original,
            raw_holdings=holdings, factors=factors, quantity_steps=steps)[1]
    assert native.describe().symbols == list(ASSETS)
