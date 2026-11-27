"""Run the native GPU Monte Carlo implementation inside a Colab kernel."""

import argparse
import json
import platform
import subprocess
import sys
from importlib.metadata import metadata, version

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from pathlib import Path
from time import perf_counter

def validate_dependencies(loaded):
    package = metadata("factorlab")
    if platform.python_version() not in SpecifierSet(package["Requires-Python"]):
        raise RuntimeError("Research Python violates FactorLab Requires-Python")
    for text in package.get_all("Requires-Dist", []):
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({"extra": "monte-carlo-cuda"}):
            continue
        actual = loaded[requirement.name] if requirement.name in loaded else version(requirement.name)
        if actual not in requirement.specifier:
            raise RuntimeError(f"{requirement.name} {actual} violates {requirement.specifier}")


def main():
    import cupy as cp
    import numpy as np
    from factorlab.compute import ComputeConfig
    from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
    parser = argparse.ArgumentParser()
    parser.add_argument("--paths", type=int, default=1_000_000)
    parser.add_argument("--steps", type=int, default=252)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", default="/content/factorlab-result")
    args = parser.parse_args()
    validate_dependencies({"numpy": np.__version__, "cupy-cuda13x": cp.__version__})
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
        "python": platform.python_version(), "numpy": np.__version__, "cupy": cp.__version__,
        "factorlab": version("factorlab"), "pid": __import__("os").getpid(),
        "cuda_runtime": cp.cuda.runtime.runtimeGetVersion(),
        "cuda_driver": cp.cuda.runtime.driverGetVersion(),
        "dependency_constraints_validated": True,
        "dependencies": subprocess.check_output(
            [sys.executable, "-m", "pip", "freeze"], text=True),
        "gpu": subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                        "--format=csv,noheader"], text=True).strip(),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"paths": args.paths, "seconds": elapsed, "output": str(output)}), flush=True)


if __name__ == "__main__":
    main()
