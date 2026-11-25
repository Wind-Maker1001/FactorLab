"""Fixed-weight, each-step rebalanced risk scenarios; zero cash yield and costs."""
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from importlib import import_module
import hashlib
import json
import platform
from time import perf_counter

import numpy as np

from factorlab.compute import (BackendCapability, BackendStatus, ComputeConfig,
    ComputeMemoryError, Device, probe_runtime, resolve_compute)
from factorlab.dataset.identity import json_hash
from factorlab.evaluation.research import EVIDENCE, MetricValue


@dataclass(frozen=True)
class MonteCarloConfig:
    """Simulation horizon in steps; GBM rates use the same time unit as ``dt``.

    ``batch_size=None`` plans bounded streaming batches. Batch size is part of
    RNG reproducibility. ``save_full_paths`` retains histories in host RAM only.
    """

    model: str = "gbm"
    n_paths: int = 10000
    n_steps: int = 252
    seed: int = 0
    dt: float = 1 / 252
    initial_value: float = 1.0
    batch_size: int | None = None
    block_size: int = 20
    quantiles: tuple = (.01, .05, .5, .95, .99)
    confidence: float = .95
    drawdown_threshold: float = .2
    save_full_paths: bool = False
    compute: ComputeConfig = field(default_factory=ComputeConfig)

    def __post_init__(self):
        if self.model not in {"gbm", "moving_block_bootstrap"}:
            raise ValueError("unknown Monte Carlo model")
        for name in ("n_paths", "n_steps", "block_size"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name + " must be a positive integer")
        if self.batch_size is not None and (type(self.batch_size) is not int or self.batch_size <= 0):
            raise ValueError("batch_size must be positive or None")
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("seed must be in [0, 2**32)")
        if any(not np.isfinite(x) or x <= 0 for x in (self.dt, self.initial_value)):
            raise ValueError("dt and initial_value must be positive finite numbers")
        if not 0 < self.confidence < 1 or not 0 <= self.drawdown_threshold <= 1:
            raise ValueError("invalid confidence or drawdown threshold")
        if not self.quantiles or any(not np.isfinite(q) or not 0 <= q <= 1 for q in self.quantiles):
            raise ValueError("quantiles must be nonempty probabilities")
        if type(self.save_full_paths) is not bool:
            raise ValueError("save_full_paths must be boolean")


@dataclass(frozen=True)
class MonteCarloInput:
    """Ordered asset vectors or aligned historical simple-return rows.

    Bootstrap observation and availability timestamps must not exceed ``as_of``.
    Weights are long-only; residual cash earns zero, and rebalancing is costless.
    Weight sums in (1, 1 + 1e-12] are normalized as absolute roundoff tolerance.
    """

    weights: object
    drift: object = None
    volatility: object = None
    correlation: object = None
    historical_returns: object = None
    timestamps: object = None
    available_at: object = None
    as_of: object = None
    assets: tuple | None = None
    initial_prices: object = None


@dataclass(frozen=True)
class MonteCarloResult:
    terminal_values: np.ndarray
    maximum_drawdowns: np.ndarray
    terminal_quantiles: dict
    drawdown_quantiles: dict
    var: float
    cvar: float
    loss_probability: float
    drawdown_breach_probability: float
    metadata: dict
    portfolio_paths: np.ndarray | None = None
    asset_paths: np.ndarray | None = None

    @property
    def terminal_returns(self):
        return self.terminal_values / self.metadata["config"]["initial_value"] - 1

    def to_dict(self):
        """Compact research report; never serializes paths or sample vectors."""
        return {"schema_version": 1, "kind": "risk_scenario", "metadata": self.metadata,
            "metrics": {key: asdict(MetricValue(float(value), "OK")) for key, value in {
                "var": self.var, "cvar": self.cvar,
                "terminal_nav_mean": self.terminal_values.mean(),
                "maximum_drawdown_mean": self.maximum_drawdowns.mean(),
                "loss_probability": self.loss_probability,
                "drawdown_breach_probability": self.drawdown_breach_probability}.items()},
            "terminal_quantiles": self.terminal_quantiles,
            "drawdown_quantiles": self.drawdown_quantiles,
            "evidence": list(EVIDENCE), "financial_evidence": False}


def _loss_tail(losses, confidence):
    """Linear VaR and fractional-boundary worst-tail mean, including negative losses."""
    var = float(np.quantile(losses, confidence))
    n = len(losses)
    tail_mass = (1 - confidence) * n
    whole = int(np.floor(tail_mass))
    fraction = tail_mass - whole
    needed = whole + (1 if fraction else 0)
    if needed < n:
        kth = n - needed
        descending = np.sort(np.partition(losses, kth)[kth:])[::-1]
    else:
        descending = np.sort(losses)[::-1]
    tail_sum = descending[:whole].sum()
    if fraction:
        tail_sum += fraction * descending[whole]
    return var, float(tail_sum / tail_mass)


def _vector(value, n, name):
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (n,) or not np.isfinite(array).all():
        raise ValueError(name + " must be a finite asset vector")
    return array


def _prepare(config, inputs):
    w = np.asarray(inputs.weights, dtype=np.float64)
    if w.ndim != 1 or not len(w) or not np.isfinite(w).all() or (w < 0).any():
        raise ValueError("weights must be nonnegative with sum at most one")
    total = w.sum()
    if total > 1 + 1e-12:
        raise ValueError("weights must be nonnegative with sum at most one")
    if total > 1:
        w = w / total
    n = len(w)
    assets = tuple(str(i) for i in range(n)) if inputs.assets is None else tuple(inputs.assets)
    if len(assets) != n or len(set(assets)) != n or any(not isinstance(a, str) or not a for a in assets):
        raise ValueError("assets must be unique nonempty names")
    prices = np.ones(n) if inputs.initial_prices is None else _vector(inputs.initial_prices, n, "initial_prices")
    if (prices <= 0).any():
        raise ValueError("initial_prices must be positive")
    data = {"weights": w, "initial_prices": prices}
    receipt = {"sample_interval": None, "as_of": None, "correlation_correction": None,
        "initial_prices": prices.tolist()}
    if config.model == "gbm":
        drift = _vector(inputs.drift, n, "drift")
        vol = _vector(inputs.volatility, n, "volatility")
        if (vol < 0).any():
            raise ValueError("volatility must be nonnegative")
        corr = np.eye(n) if inputs.correlation is None else np.asarray(inputs.correlation, dtype=np.float64)
        if corr.shape != (n, n) or not np.isfinite(corr).all() or not np.allclose(corr, corr.T, rtol=0, atol=1e-12) or not np.allclose(np.diag(corr), 1, rtol=0, atol=1e-12):
            raise ValueError("correlation must be symmetric with unit diagonal")
        symmetric = (corr + corr.T) / 2
        eig, vectors = np.linalg.eigh(symmetric)
        if eig.min() < -1e-12:
            raise ValueError("correlation must be positive semidefinite")
        root = vectors * np.sqrt(np.maximum(eig, 0))
        effective = root @ root.T
        if not np.array_equal(corr, symmetric) or (eig < 0).any():
            receipt["correlation_correction"] = {
                "method": "symmetrize_then_clip_eigenvalues_in_absolute_tolerance_1e-12",
                "minimum_original_eigenvalue": float(eig.min()),
                "maximum_absolute_change": float(np.max(np.abs(effective - corr))),
                "effective_correlation": effective.tolist()}
        receipt["gbm_parameters"] = {"drift": drift.tolist(), "volatility": vol.tolist(),
            "correlation": corr.tolist()}
        data.update(drift=drift, volatility=vol, correlation=corr, root=root)
    else:
        returns = np.asarray(inputs.historical_returns, dtype=np.float64)
        if returns.ndim != 2 or returns.shape[1] != n or not np.isfinite(returns).all() or (returns < -1).any():
            raise ValueError("historical_returns must be finite simple returns >= -1 of shape (observations, assets)")
        if inputs.timestamps is None or inputs.available_at is None or inputs.as_of is None:
            raise ValueError("bootstrap requires timestamps, available_at and as_of")
        times = np.asarray(inputs.timestamps, dtype="datetime64[ns]")
        available = np.asarray(inputs.available_at, dtype="datetime64[ns]")
        cutoff = np.datetime64(inputs.as_of, "ns")
        if times.shape != (len(returns),) or available.shape != times.shape or np.isnat(times).any() or np.isnat(available).any() or np.isnat(cutoff):
            raise ValueError("invalid PIT timestamp vectors")
        if (np.diff(times) <= np.timedelta64(0, "ns")).any() or (available < times).any():
            raise ValueError("observations must increase and availability cannot precede observation")
        if (times > cutoff).any() or (available > cutoff).any():
            raise ValueError("historical training data must be known no later than as_of")
        if len(returns) < config.block_size:
            raise ValueError("history is shorter than block_size")
        starts = np.arange(len(returns) - config.block_size + 1, dtype=np.int64)
        receipt.update(sample_interval=[str(times[0]), str(times[-1])], as_of=str(cutoff))
        data.update(returns=returns, starts=starts, timestamps=times.astype(np.int64),
                    available_at=available.astype(np.int64), as_of=np.asarray(cutoff.astype(np.int64)))
    digest = hashlib.sha256(bytes.fromhex(json_hash({"assets": assets})))
    for key, value in sorted(data.items()):
        value = np.ascontiguousarray(value)
        digest.update(key.encode())
        digest.update(str((value.shape, value.dtype.str)).encode())
        digest.update(value.tobytes())
    return data, assets, digest.hexdigest(), receipt


def _cuda_reducer(cp):
    return cp.RawKernel(r'''
    extern "C" __global__ void reduce_paths(
        const double* gross, const long long paths, const long long steps,
        const double initial, double* terminal, double* drawdown,
        double* nav, const int save) {
        const long long i = (long long)blockDim.x * blockIdx.x + threadIdx.x;
        if (i >= paths) return;
        double wealth = initial, peak = initial, mdd = 0.0;
        if (save) nav[i * (steps + 1)] = initial;
        for (long long t = 0; t < steps; ++t) {
            wealth *= gross[t * paths + i];
            peak = fmax(peak, wealth);
            mdd = fmax(mdd, 1.0 - wealth / peak);
            if (save) nav[i * (steps + 1) + t + 1] = wealth;
        }
        terminal[i] = wealth;
        drawdown[i] = mdd;
    }
    ''', "reduce_paths", options=("--std=c++11",))


def _gpu_probe(config):
    def check(cp):
        try:
            if cp.cuda.runtime.getDeviceCount() == 0:
                return BackendStatus(False, "no CUDA device", driver_visible=False)
            # Probe within a private pool too: never alter the caller's default pool.
            pool = cp.cuda.MemoryPool()
            if config.gpu_memory_limit_mb is not None:
                pool.set_limit(size=config.gpu_memory_limit_mb * 1024**2)
            with cp.cuda.using_allocator(pool.malloc):
                rng = cp.random.RandomState(0)
                value = cp.expm1(rng.standard_normal((2, 2)) @ cp.eye(2))
                selected = value[cp.asarray([0, 1])]
                cp.maximum(selected, 0).sum().item()
                terminal, drawdown, nav = cp.empty(2), cp.empty(2), cp.empty(0)
                _cuda_reducer(cp)((1,), (32,), (cp.ones((2, 2)), np.int64(2), np.int64(2),
                    np.float64(1), terminal, drawdown, nav, np.int32(0)))
                cp.cuda.get_current_stream().synchronize()
                del value, selected, rng, terminal, drawdown, nav
            pool.free_all_blocks()
            return BackendStatus(True, driver_visible=True, runtime_available=True)
        except (cp.cuda.runtime.CUDARuntimeError, cp.cuda.memory.OutOfMemoryError) as exc:
            return BackendStatus(False, str(exc), driver_visible=True)
    return probe_runtime("cupy", check)


class MonteCarloRunner:
    """Positive MDD includes starting wealth; execution failures never retry on CPU."""

    def __init__(self, config=MonteCarloConfig()):
        self.config = config

    def run(self, inputs):
        started = perf_counter()
        c = self.config
        data, assets, sample_hash, input_receipt = _prepare(c, inputs)
        resolved = resolve_compute((BackendCapability(Device.GPU, "cupy-cuda", _gpu_probe),
            BackendCapability(Device.CPU, "numpy", lambda _: BackendStatus(True, version=np.__version__, runtime_available=True))), c.compute)
        gpu = resolved.device == Device.GPU
        xp = import_module("cupy") if gpu else np
        pool = xp.cuda.MemoryPool() if gpu else None
        budget = c.compute.gpu_memory_limit_mb * 1024**2 if c.compute.gpu_memory_limit_mb is not None else None
        if gpu and budget is not None:
            pool.set_limit(size=budget)
        n = len(data["weights"])
        persistent = sum(v.nbytes + 512 for v in data.values()) + 65536
        # Batch-local shocks, GEMM temporaries, gathered returns, indices and NAV
        # workspace; allocator rounding and scan workspace have explicit headroom.
        per_path = 8 * ((8 * n + 16) * c.n_steps + 128) + 4096
        batch = min(c.batch_size or 4096, c.n_paths)
        if gpu:
            free, _ = xp.cuda.runtime.memGetInfo()
            planning_budget = min(budget, int(free * .8)) if budget is not None else int(free * .8)
            batch = min(batch, (planning_budget - persistent) // per_path)
            if batch < 1:
                raise ComputeMemoryError("GPU budget cannot fit one Monte Carlo path")
        terminal = np.empty(c.n_paths)
        drawdowns = np.empty(c.n_paths)
        paths = np.empty((c.n_paths, c.n_steps + 1)) if c.save_full_paths else None
        asset_paths = np.empty((c.n_paths, c.n_steps + 1, n)) if c.save_full_paths else None
        def simulate():
            peak_pool = 0
            with xp.cuda.using_allocator(pool.malloc) if gpu else nullcontext():
                arrays = {k: xp.asarray(v) for k, v in data.items() if k not in {"timestamps", "available_at", "as_of", "correlation"}}
                rng = xp.random.RandomState(c.seed)
                def host(a):
                    return xp.asnumpy(a) if gpu else a
                reducer = _cuda_reducer(xp) if gpu else None
                offsets = xp.arange(c.n_steps, dtype=xp.int64)
                for begin in range(0, c.n_paths, batch):
                    end = min(begin + batch, c.n_paths)
                    size = end - begin
                    if c.model == "gbm":
                        returns = rng.standard_normal((c.n_steps * size, n)) @ arrays["root"].T
                        returns *= arrays["volatility"] * np.sqrt(c.dt)
                        returns += (arrays["drift"] - .5 * arrays["volatility"] ** 2) * c.dt
                        xp.expm1(returns, out=returns)
                        returns = returns.reshape(c.n_steps, size, n)
                    else:
                        blocks = (c.n_steps + c.block_size - 1) // c.block_size
                        chosen = arrays["starts"][rng.randint(0, len(data["starts"]), size=(blocks, size))]
                        indices = chosen[offsets // c.block_size] + (offsets % c.block_size)[:, None]
                        returns = arrays["returns"][indices]
                        del chosen, indices
                    gross = (returns.reshape(-1, n) @ arrays["weights"]).reshape(c.n_steps, size)
                    gross += 1
                    if gpu:
                        wealth, mdd = xp.empty(size), xp.empty(size)
                        nav = xp.empty((size, c.n_steps + 1)) if paths is not None else xp.empty(0)
                        reducer(((size + 127) // 128,), (128,), (gross,
                            np.int64(size), np.int64(c.n_steps), np.float64(c.initial_value),
                            wealth, mdd, nav, np.int32(paths is not None)))
                        terminal[begin:end], drawdowns[begin:end] = host(wealth), host(mdd)
                        if paths is not None:
                            paths[begin:end] = host(nav)
                        del wealth, mdd, nav
                    else:
                        nav = np.cumprod(gross, axis=0) * c.initial_value
                        peak = np.maximum.accumulate(nav, axis=0)
                        np.maximum(peak, c.initial_value, out=peak)
                        terminal[begin:end] = nav[-1]
                        drawdowns[begin:end] = np.max(1 - nav / peak, axis=0)
                        if paths is not None:
                            paths[begin:end, 0] = c.initial_value
                            paths[begin:end, 1:] = nav.T
                        del nav, peak
                    if paths is not None:
                        asset_paths[begin:end, 0] = data["initial_prices"]
                        prices = xp.cumprod(1 + returns, axis=0) * arrays["initial_prices"]
                        asset_paths[begin:end, 1:] = host(prices.transpose(1, 0, 2))
                        del prices
                    if gpu:
                        peak_pool = max(peak_pool, pool.total_bytes())
                    del returns, gross
                if gpu:
                    xp.cuda.get_current_stream().synchronize()
                    peak_pool = max(peak_pool, pool.total_bytes())
            return peak_pool
        try:
            peak_pool = simulate()
        except ((xp.cuda.memory.OutOfMemoryError, xp.cuda.runtime.CUDARuntimeError) if gpu else ()) as exc:
            if isinstance(exc, xp.cuda.runtime.CUDARuntimeError) and exc.status != 2:
                raise
            raise ComputeMemoryError("Monte Carlo CUDA allocation failed; partial output discarded") from exc
        finally:
            if gpu:
                pool.free_all_blocks()
        if not np.isfinite(terminal).all() or not np.isfinite(drawdowns).all():
            raise FloatingPointError("simulation produced nonfinite wealth")
        losses = 1 - terminal / c.initial_value
        var, cvar = _loss_tail(losses, c.confidence)
        evidence = resolved.to_dict()
        evidence["execution"] = {"backend": resolved.backend, "batch_size": batch,
            "private_pool_limit_enforced": bool(gpu and budget is not None),
            "private_pool_limit_bytes": budget if gpu else None,
            "private_pool_peak_bytes": peak_pool if gpu else None,
            "budget_scope": "private CuPy allocator pool only; CUDA context, cuRAND and cuBLAS external allocations are not bounded",
            "planned_persistent_bytes": persistent, "planned_bytes_per_path": per_path}
        metadata = {"config": asdict(c), "sample_hash": sample_hash, "assets": list(assets),
            "evaluation": "MONTE_CARLO_RISK_SCENARIO_V1", "compute": evidence,
            "method_version": 1, "input": input_receipt,
            "seed": c.seed, "rng": "CuPy RandomState/cuRAND XORWOW" if gpu else "NumPy RandomState/MT19937",
            "rng_backend": resolved.backend, "rng_version": xp.__version__,
            "python_version": platform.python_version(), "numpy_version": np.__version__,
            "cuda_runtime_version": xp.cuda.runtime.runtimeGetVersion() if gpu else None,
            "cuda_driver_version": xp.cuda.runtime.driverGetVersion() if gpu else None,
            "elapsed_seconds": perf_counter() - started,
            "reproducibility": "same backend, versions, configuration and batch plan; batch size changes the random sequence",
            "risk_definition": {"loss": "1 - terminal_nav / initial_nav; negative loss is gain",
                "horizon_steps": c.n_steps, "dt": c.dt, "confidence": c.confidence,
                "var_quantile": "linear (Hyndman-Fan type 7)",
                "cvar": "mean of worst (1-confidence)*n losses with fractional boundary observation",
                "drawdown": "positive fraction below running peak including initial NAV",
                "breach": "maximum_drawdown strictly greater than threshold",
                "threshold": c.drawdown_threshold},
            "portfolio": {"weights": data["weights"].tolist(), "cash_weight": float(max(0, 1-data["weights"].sum())),
                "rebalancing": "each_step", "cash_return": 0, "costs": 0},
            "assumptions": ["RISK_SCENARIO_ONLY", "NOT_EVENT_DRIVEN_EXECUTION",
                "constant GBM parameters or stationary resampled historical blocks",
                "sampling uncertainty excludes model uncertainty"]}
        return MonteCarloResult(terminal, drawdowns,
            {q: float(np.quantile(terminal, q)) for q in c.quantiles},
            {q: float(np.quantile(drawdowns, q)) for q in c.quantiles}, var, cvar,
            float(np.mean(losses > 0)), float(np.mean(drawdowns > c.drawdown_threshold)),
            metadata, paths, asset_paths)

    def record(self, result, tracker, experiment_id, run_id, *, code, command, environment=None, qualification="SYNTHETIC"):
        """Append to a matching, caller-created experiment; preserve actual provenance."""
        definition = tracker.get_experiment(experiment_id)["payload"]
        expected = {"monte_carlo": result.metadata["config"], "sample_hash": result.metadata["sample_hash"]}
        expected = json.loads(json.dumps(expected))
        if definition["parameters"] != expected or definition["model"] != "monte_carlo/" + self.config.model:
            raise ValueError("experiment configuration/sample identity mismatch")
        if result.metadata["config"] != asdict(self.config):
            raise ValueError("result configuration does not belong to runner")
        return tracker.append_run(experiment_id, run_id, code=code,
            environment={**(environment or {}), "compute": result.metadata["compute"],
                "monte_carlo": result.metadata},
            command=command, status="SUCCEEDED", qualification=qualification,
            portfolio_metrics={"var": result.var, "cvar": result.cvar,
                "loss_probability": result.loss_probability,
                "drawdown_breach_probability": result.drawdown_breach_probability},
            validation={"sample_hash": result.metadata["sample_hash"],
                "evaluation": {"kind": result.metadata["evaluation"],
                    "risk_definition": result.metadata["risk_definition"],
                    "portfolio": result.metadata["portfolio"]},
                "report": result.to_dict()})


__all__ = ["MonteCarloConfig", "MonteCarloInput", "MonteCarloResult", "MonteCarloRunner"]
