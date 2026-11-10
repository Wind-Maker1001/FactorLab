"""Check saved-config replay and past-decision invariance on private real panels."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pandas as pd

from factorlab.data.hub import DataHub
from factorlab.strategies.pairs_real_cli import PreparedCsmarAdapter, execute
from factorlab.strategies.pairs_statistics import SelectionConfig
from factorlab.strategies.pairs_trading import PairTradingConfig, read_pair_panels, run_pair_research
from factorlab.validation.splitter import ExpandingWalkForwardSplitter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "result", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--cutoff", required=True)
    args = parser.parse_args()
    config = json.loads((args.result / "config.json").read_text(encoding="utf-8"))
    tables, metadata = execute(config, candidate=args.candidate, output=args.output)
    for name in tables:
        left = pd.read_csv(args.result / (name + ".csv"))
        right = pd.read_csv(args.output / (name + ".csv"))
        pd.testing.assert_frame_equal(left, right)
    original_models = json.loads((args.result / "models.json").read_text(encoding="utf-8"))
    replay_models = json.loads((args.output / "models.json").read_text(encoding="utf-8"))
    if original_models != replay_models:
        raise AssertionError("saved-config model replay differs")
    adapter = PreparedCsmarAdapter(args.candidate)
    symbols = sorted({s for pair in config["pairs"] for s in pair["symbols"]})
    opens, closes, _ = read_pair_panels(DataHub({"csmar": adapter}), "csmar", symbols,
        start=config["start"], end=config["end"], close_field=config["price_field"])
    cutoff = pd.Timestamp(args.cutoff)
    verified = []
    for pair in config["pairs"]:
        y, x = sorted(pair["symbols"])
        pair_id = y + "__" + x
        altered = closes[[y, x]].copy()
        altered.loc[altered.index > cutoff, y] *= 1.35
        changed = run_pair_research(opens[[y, x]], altered,
            selection=SelectionConfig(train_end=config["start"], **config["selection"]),
            strategy=PairTradingConfig(**config["strategy"]),
            splitter=ExpandingWalkForwardSplitter(**config["splitter"]), borrow={},
            entry_grid=config["entry_grid"], source_kind="REAL_CANDIDATE", statistics_only=True)
        old = tables["signals"].loc[tables["signals"].pair_id.eq(pair_id)].drop(columns="pair_id")
        if not old.empty:
            pd.testing.assert_frame_equal(old.loc[old.date.le(cutoff)].reset_index(drop=True),
                changed.signals.loc[changed.signals.date.le(cutoff)].reset_index(drop=True))
        old_selection = tables["selections"].loc[tables["selections"].pair_id.eq(pair_id)].drop(
            columns=["pair_id", "bonferroni_accepted", "high_correlation_rejected", "high_correlation_eg_rejected"])
        # Boundary metadata are unchanged; only training windows ending by the
        # cutoff are eligible for this past-decision comparison.
        mask = pd.to_datetime(old_selection.train_end).le(cutoff)
        other = changed.selections.loc[pd.to_datetime(changed.selections.train_end).le(cutoff)]
        pd.testing.assert_frame_equal(old_selection.loc[mask].reset_index(drop=True), other.reset_index(drop=True),
                                      check_dtype=False)
        verified.append(dict(pair_id=pair_id, past_training_folds=int(mask.sum()),
                             past_signal_rows=int(old.date.le(cutoff).sum()) if not old.empty else 0))
    evidence = dict(saved_config_replay="PASS", future_perturbation="PASS", cutoff=args.cutoff,
                    perturbation="Only Y closes strictly after cutoff multiplied by 1.35",
                    pairs=verified, execution=metadata["execution"],
                    snapshot_sha256=adapter.manifest["bars_sha256"])
    (args.output / "verification.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
