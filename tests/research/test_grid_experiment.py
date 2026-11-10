"""Exercise the synthetic evaluation/tracker seam and saved-event replay."""
import json

import pandas as pd
import pytest

from factorlab.experiments.grid_synthetic import run_experiment
from factorlab.strategies.grid import GridStrategy


def test_four_scenarios_evaluation_tracker_and_replay(tmp_path):
    output = run_experiment(tmp_path / "original")
    summary = pd.read_csv(output / "summary.csv")
    assert set(summary.scenario) == {"range", "rising", "falling", "gap"}
    assert set(summary.model) == {"arithmetic", "geometric", "atr", "buyhold"}
    assert len(summary) == 16
    for scenario in summary.scenario.unique():
        assert (output / scenario / "tracker-comparison.json").is_file()
        for mode in ("arithmetic", "geometric", "atr"):
            payload = json.loads((output / scenario / (mode + "-replay.json")).read_text())
            replay = GridStrategy.replay(payload)
            snapshots = json.loads((output / scenario / (mode + "-snapshots.json")).read_text())
            audit = json.loads((output / scenario / (mode + "-audit.json")).read_text())
            assert replay.snapshots == snapshots
            assert replay.events == audit
            final = snapshots[-1]
            assert final["equity"] - replay.initial_equity == pytest.approx(final["realized_pnl"] + final["unrealized_pnl"])
    reproduced = run_experiment(tmp_path / "replay", replay=output / "manifest.json")
    pd.testing.assert_frame_equal(summary.sort_values(["scenario", "model"]).reset_index(drop=True),
        pd.read_csv(reproduced / "summary.csv").sort_values(["scenario", "model"]).reset_index(drop=True))
