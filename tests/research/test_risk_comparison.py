"""Synthetic checks for budget translation, execution and descriptive pairing."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.errors import SchemaError
from factorlab.strategies.real_comparison import compare, execute_signals, NAMES
from factorlab.strategies.risk_comparison import budget_signals, observations, paired_tables, run, BLOCKED


@pytest.fixture
def account():
    config = json.loads((Path(__file__).parents[2] / 'configs/research/p5-r02-601857-2018.json').read_text())
    dates = pd.bdate_range('2017-09-01', periods=140)
    config['warmup_start'] = str(dates[0].date())
    config['common'].update(start=str(dates[60].date()), end=str(dates[-1].date()))
    close = 10 + np.sin(np.arange(140) / 5) + np.arange(140) / 200
    bars = pd.DataFrame(dict(date=dates, symbol='601857.SH', raw_open=close + .01,
        raw_close=close, raw_high=close + .1, raw_low=close - .1,
        volume=1000000, trade_status=True, is_st=False))
    return config, compare(bars, dates, config, source_kind='SYNTHETIC')


def execute(base, config, budget):
    return execute_signals(budget_signals(base['signals'], budget), base['input_bars'],
                           base['research'], config, source_kind='SYNTHETIC')


def test_preserve_intent_and_cta_risk_sizing():
    frame = pd.DataFrame(dict(target_weight=[np.nan, 0., .2, 1.], signal=['hold', 'sell', 'buy', 'buy']))
    result = budget_signals({'cta': frame}, .25)['cta']
    np.testing.assert_allclose(result.target_weight, [np.nan, 0., .05, .25], equal_nan=True)
    pd.testing.assert_series_equal(frame.signal, result.signal)
    assert frame.target_weight.iloc[2] == .2
    for invalid in (0, -1, 1.1, np.nan):
        with pytest.raises(SchemaError):
            budget_signals({'cta': frame}, invalid)


def test_full_budget_replays_shared_contract(account):
    config, base = account
    full = execute(base, config, 1.)
    for name in NAMES:
        assert base['summary'][name] == full['summary'][name]
        for kind in ('nav', 'trades', 'rejections'):
            pd.testing.assert_frame_equal(base['legs'][name][kind], full['legs'][name][kind])


def test_small_budget_reexecutes_round_lots_and_minimum_fees(account):
    config, base = account
    small = execute(base, config, .1)
    hold = small['legs']['buy_hold']
    assert hold['trades'].execution_date.iloc[0] == base['research'][0]
    assert hold['trades'].executed_shares.iloc[0] % 100 == 0
    assert hold['trades'].commission.iloc[0] == 5.
    assert hold['trades'].executed_shares.iloc[0] != base['legs']['buy_hold']['trades'].executed_shares.iloc[0] * .1
    for name in NAMES:
        nav = small['legs'][name]['nav']
        assert nav.cash.ge(0).all()
        assert nav.shares.mod(100).eq(0).all()
        np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.market_value)
        np.testing.assert_array_equal(nav.benchmark_nav, hold['nav'].strategy_nav)
    assert small['summary']['buy_hold']['cumulative_return'] != base['summary']['buy_hold']['cumulative_return'] * .1


def test_same_budget_pairing_and_cost_identity(account):
    config, base = account
    rows = []
    for budget in (.1, .25):
        result = execute(base, config, budget)
        for symbol in ('A', 'B'):
            rows.extend(observations(result, dict(symbol=symbol, year=2018, industry='synthetic'),
                                     str(budget), budget, 100000))
    tables = paired_tables(pd.DataFrame(rows))
    pairs = tables['paired_accounts']
    np.testing.assert_allclose(pairs.paired_cumulative_return,
        pairs.paired_same_fill_gross_contribution - pairs.paired_cost_to_initial_capital, atol=1e-15)
    assert pairs.loc[pairs.strategy.eq('buy_hold'), 'paired_cumulative_return'].eq(0).all()
    assert tables['paired_leave_one_security_out'].securities.eq(1).all()


def test_missing_data_cannot_produce_empirical_results(tmp_path):
    config = Path(__file__).parents[2] / 'configs/research/p7-02-risk-matched-comparison.json'
    output = tmp_path / 'blocked'
    receipt = run(config, tmp_path / 'missing', tmp_path / 'missing', tmp_path / 'missing', output)
    assert receipt['status'] == BLOCKED
    assert receipt['real_accounts_run'] == 0
    assert not (output / 'account-metrics.csv').exists()
