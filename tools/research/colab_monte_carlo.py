"""Run the native GPU Monte Carlo implementation inside a Colab kernel."""

import argparse
import json
import platform
import subprocess
from pathlib import Path
from time import perf_counter

import cupy as cp
import numpy as np

from factorlab.compute import ComputeConfig
from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=1_000_000)
    parser.add_argument("--steps", type=int, default=252)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", default="/content/factorlab-result")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir()
    config = MonteCarloConfig(
        n_paths=args.paths, n_steps=args.steps, seed=args.seed,
        compute=ComputeConfig(device="gpu", allow_fallback=False),
    )
    started = perf_counter()
    result = MonteCarloRunner(config).run(MonteCarloInput(
        weights=np.array([1.0]), drift=np.array([0.08]),
        volatility=np.array([0.2]), correlation=np.eye(1),
    ))
    elapsed = perf_counter() - started
    np.savez_compressed(output / "samples.npz", terminal_values=result.terminal_values,
                        maximum_drawdowns=result.maximum_drawdowns)
    report = result.to_dict()
    report["execution"] = {
        "commit": args.commit, "elapsed_seconds": elapsed,
        "python": platform.python_version(), "cupy": cp.__version__,
        "dependencies": subprocess.check_output(
            ["python", "-m", "pip", "freeze"], text=True),
        "gpu": subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                        "--format=csv,noheader"], text=True).strip(),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"paths": args.paths, "seconds": elapsed, "output": str(output)}), flush=True)


if __name__ == "__main__":
    main()
