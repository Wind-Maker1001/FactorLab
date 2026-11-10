import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.backtest.target import run_target_positions
from factorlab.errors import ConfigurationError, SchemaError
from factorlab.strategies.custom import load_strategy, run_custom_strategy, save_custom_run, replay_custom_run
from factorlab.strategies.dual_ma_cli import synthetic_bars

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/custom_strategy"


def setup_config(tmp_path, *, cls="MyStrategy", parameters=None, source=None):
    source_path = tmp_path / "strategy.py"
    source_path.write_text(source or (EXAMPLE / "ma_strategy.py").read_text(), encoding="utf-8")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(dict(strategy={"file": "strategy.py", "class": cls,
        "parameters": {"window": 5} if parameters is None else parameters},
        research={"symbol": "601857.SH", "start": "2018-01-01", "end": "2018-03-30"})))
    return path


def inputs(path):
    _, config, _ = load_strategy(path)
    return synthetic_bars(config)


def test_execution_and_replay(tmp_path):
    path = setup_config(tmp_path)
    bars, calendar = inputs(path)
    result = run_custom_strategy(bars, calendar, path, source_kind="SYNTHETIC")
    direct = run_target_positions(result["signals"], bars, pd.DatetimeIndex(result["signals"].date),
                                  ExecutionBacktestConfig(initial_cash=100000.0), result["fees"])
    for key in ("trades", "rejections"):
        pd.testing.assert_frame_equal(result["strategy"][key], direct[key])
    pd.testing.assert_frame_equal(result["strategy"]["nav"][direct["nav"].columns], direct["nav"])
    trades = result["strategy"]["trades"]
    assert len(trades) > 0
    positions = pd.Series(np.arange(len(calendar)), index=calendar)
    assert (trades.execution_date.map(positions) == trades.signal_date.map(positions) + 1).all()
    nav = result["strategy"]["nav"]
    np.testing.assert_allclose(nav.portfolio_value, nav.cash + nav.shares * bars.set_index("date").loc[nav.date].raw_close.to_numpy())
    np.testing.assert_allclose(nav.strategy_nav, nav.portfolio_value / 100000)
    assert trades.transaction_cost.sum() > 0
    flow = np.where(trades.side.eq("buy"), -trades.trade_notional, trades.trade_notional) - trades.transaction_cost
    np.testing.assert_allclose(nav.cash.iloc[-1], 100000.0 + flow.sum())
    provenance = dict(code={"commit": None, "tree": None}, environment={})
    output = save_custom_run(result, tmp_path / "saved", provenance=provenance, command=["pytest"])
    replay = replay_custom_run(output)
    for key in ("nav", "trades", "rejections"):
        pd.testing.assert_frame_equal(result["strategy"][key], replay["strategy"][key])
    saved = json.loads((output / "tracker/experiments/custom.json").read_text())
    assert saved["payload"]["parameters"]["strategy"]["parameters"] == {"window": 5}
    (tmp_path / "strategy.py").write_text("raise RuntimeError('changed')")
    with pytest.raises(ConfigurationError, match="identity changed"):
        replay_custom_run(output)


def test_future_perturbation_and_builtin(tmp_path):
    path = setup_config(tmp_path)
    bars, calendar = inputs(path)
    source_path = tmp_path / "strategy.py"
    source_path.write_text(source_path.read_text().replace(
        "        average = bars.adj_close", "        assert 'future_return' not in bars\n        assert bars.date.max() == calendar[-1]\n        average = bars.adj_close"))
    bars['future_return'] = 999.0
    original = run_custom_strategy(bars, calendar, path)
    cutoff = calendar[-20]
    changed = bars.copy()
    mask = changed.date > cutoff
    for name in ("adj_close", "raw_open", "raw_close", "raw_high", "raw_low"):
        changed.loc[mask, name] *= 1.05
    perturbed = run_custom_strategy(changed, calendar, path)
    pd.testing.assert_frame_equal(original["signals"].query("date <= @cutoff"), perturbed["signals"].query("date <= @cutoff"))
    path = setup_config(tmp_path, cls="BuiltinDualMA")
    bars, calendar = inputs(path)
    adapted = run_custom_strategy(bars, calendar, path)
    from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals
    expected = moving_average_signals(bars, calendar, DualMAConfig(short_window=1, long_window=5,
        start="2018-01-01", end="2018-03-30"))
    np.testing.assert_allclose(adapted["signals"].target_weight, expected.target_weight, equal_nan=True)


@pytest.mark.parametrize("parameters", [{"window": True}, {"window": "5"}, {"window": 1}, {"unknown": 5}])
def test_parameter_rejection(tmp_path, parameters):
    with pytest.raises(ConfigurationError):
        load_strategy(setup_config(tmp_path, parameters=parameters))


@pytest.mark.parametrize("body, message", [
    ("result['target_weight'] = -1.0", "shorting unsupported"),
    ("result['target_weight'] = 2.0", "target_weight"),
    ("result['target_weight'] = 'bad'", "target_weight"),
    ("result['target_weight'] = True", "target_weight"),
    ("result['target_weight'] = float('inf')", "target_weight"),
    ("result = bars[['date', 'symbol']].assign(target_weight=0.0).iloc[::-1]", "ordered"),
    ("result['symbol'] = '600000.SH'", "security"),
    ("result = pd.concat([result, result])", "unique"),
    ("result['date'] += pd.Timedelta(days=10)", "calendar"),
    ("result = result.iloc[:0]", "current session"),
    ("raise RuntimeError('strategy bug')", "custom strategy Broken failed"),
])
def test_invalid_targets(tmp_path, body, message):
    source = '''import pandas as pd
class Broken:
    required_fields = ('adj_close',)
    warmup_sessions = 5
    def __init__(self, window: int = 5):
        pass
    def generate_targets(self, bars, calendar):
        result = bars[['date', 'symbol']].tail(1).copy()
        result['target_weight'] = float('nan')
        BODY
        return result
'''.replace("BODY", body)
    path = setup_config(tmp_path, cls="Broken", source=source)
    bars, calendar = inputs(path)
    with pytest.raises(SchemaError, match=message):
        run_custom_strategy(bars, calendar, path)


def test_warmup_and_admission_fields(tmp_path):
    path = setup_config(tmp_path)
    bars, calendar = inputs(path)
    with pytest.raises(SchemaError, match="warmup"):
        run_custom_strategy(bars.iloc[10:], calendar[10:], path)
    config = json.loads(path.read_text())
    config["research"]["allow_short"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(ConfigurationError, match="research fields"):
        load_strategy(path)
