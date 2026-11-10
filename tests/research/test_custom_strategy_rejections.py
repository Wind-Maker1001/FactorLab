import pandas as pd

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.backtest.target import run_target_positions
from factorlab.strategies.custom import run_custom_strategy
from tests.research.test_custom_strategy import setup_config, inputs


def test_suspended_execution_preserves_rejections(tmp_path):
    path = setup_config(tmp_path)
    bars, calendar = inputs(path)
    initial = run_custom_strategy(bars, calendar, path)
    day = initial['strategy']['trades'].execution_date.iloc[0]
    bars.loc[bars.date == day, 'trade_status'] = False
    result = run_custom_strategy(bars, calendar, path)
    direct = run_target_positions(result['signals'], bars, pd.DatetimeIndex(result['signals'].date),
                                  ExecutionBacktestConfig(initial_cash=100000.0), result['fees'])
    assert len(result['strategy']['rejections']) > 0
    pd.testing.assert_frame_equal(result['strategy']['rejections'], direct['rejections'])
    pd.testing.assert_frame_equal(result['strategy']['trades'], direct['trades'])
