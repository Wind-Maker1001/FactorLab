"""Run small synthetic CPU scenarios and optionally retain immutable local evidence."""
import argparse
from dataclasses import asdict
from pathlib import Path

from factorlab.compute import ComputeConfig
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.monte_carlo import (
    MonteCarloConfig, MonteCarloInput, MonteCarloRunner,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "gpu", "auto"), default="cpu")
    parser.add_argument("--store", type=Path, help="optional local ExperimentTracker directory")
    parser.add_argument("--experiment-id", default="synthetic-monte-carlo")
    parser.add_argument("--run-id", default="seed-42")
    args = parser.parse_args()
    config = MonteCarloConfig(n_paths=256, n_steps=20, seed=42, save_full_paths=True,
                             compute=ComputeConfig(device=args.device))
    inputs = MonteCarloInput(assets=("synthetic_a", "synthetic_b"),
        initial_prices=[100., 50.], weights=[.6, .3], drift=[.07, .04],
        volatility=[.2, .15], correlation=[[1., .3], [.3, 1.]])
    runner = MonteCarloRunner(config)
    result = runner.run(inputs)
    print(f"VaR={result.var:.6f}, CVaR={result.cvar:.6f}, loss probability={result.loss_probability:.3f}")
    print(f"Drawdown breach probability={result.drawdown_breach_probability:.3f}")
    print("Saved portfolio/asset shapes:", result.portfolio_paths.shape, result.asset_paths.shape)
    if args.store is not None:
        tracker = ExperimentTracker(args.store)
        definition = {
            "model": "monte_carlo/" + config.model,
            "parameters": {"monte_carlo": asdict(config), "sample_hash": result.metadata["sample_hash"]},
            "factors": [], "dataset": {"source": "explicit synthetic GBM parameters"},
            "label": {"kind": "risk_scenario"},
            "windows": {"train": None, "validation": None, "test": None},
            "walk_forward": {}, "costs": {"transaction_cost": 0, "cash_return": 0},
        }
        try:
            existing = tracker.get_experiment(args.experiment_id)
        except FileNotFoundError:
            tracker.create_experiment(args.experiment_id, definition)
        else:
            import json
            if existing["payload"] != json.loads(json.dumps(definition)):
                raise ValueError("existing experiment has different scenario identity")
        runner.record(result, tracker, args.experiment_id, args.run_id,
            **capture_provenance(Path(__file__).resolve().parents[1]),
            command=["python", "examples/monte_carlo.py", "--device", args.device,
                     "--store", str(args.store), "--experiment-id", args.experiment_id,
                     "--run-id", args.run_id], qualification="SYNTHETIC")


if __name__ == "__main__":
    main()
