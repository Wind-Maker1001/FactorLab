"""Run from the checkout with PYTHONPATH=src; output must not exist."""
from pathlib import Path
import sys

from factorlab.experiments.provenance import capture_provenance
from factorlab.strategies.custom import load_strategy, run_custom_strategy, save_custom_run, replay_custom_run
from factorlab.strategies.dual_ma_cli import synthetic_bars

config_path = Path(__file__).with_name("config.json")
_, config, _ = load_strategy(config_path)
bars, calendar = synthetic_bars(config)
result = run_custom_strategy(bars, calendar, config_path, source_kind="SYNTHETIC")
output = save_custom_run(result, sys.argv[1], provenance=capture_provenance(Path(__file__).resolve().parents[2]),
                         command=[sys.executable, *sys.argv])
replayed = replay_custom_run(output)
for key in ("nav", "trades", "rejections"):
    assert result["strategy"][key].equals(replayed["strategy"][key])
print(result["safety"])
print("P5_S01_ENGINEERING_ACCEPTED", output)
print(result["evaluations"]["strategy"].to_dict())
