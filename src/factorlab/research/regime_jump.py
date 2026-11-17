"""CPU-only, close-only risk scenarios; no market probability qualification."""
from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np
import pandas as pd

from factorlab.dataset.identity import json_hash

VERSION = 1
STATES = ('low_down', 'low_up', 'high_down', 'high_up')


class CalibrationError(ValueError):
    """Insufficient historical support; parameterized Jump Stress remains usable."""


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')


def _time(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('timestamps must be nonmissing and timezone-aware')
    return stamp.tz_convert('UTC')


def _vector(value, n, name):
    result = np.array(value, dtype=float, copy=True)
    if result.shape != (n,) or not np.isfinite(result).all():
        raise ValueError(f'{name} must be a finite asset vector')
    return result


def _probabilities(value, shape, name):
    result = np.array(value, dtype=float, copy=True)
    if (result.shape != shape or not np.isfinite(result).all() or (result < 0).any()
            or not np.allclose(result.sum(axis=-1), 1., rtol=0, atol=1e-12)):
        raise ValueError(f'{name} must contain nonnegative probabilities summing to one')
    return result / result.sum(axis=-1, keepdims=True)


def _readonly(value):
    result = np.array(value, copy=True)
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class PathPlan:
    n_paths: int = 16
    n_steps: int = 168
    batch_size: int = 8
    seed: int = 710
    max_batch_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for name in ('n_paths', 'n_steps', 'batch_size', 'max_batch_bytes'):
            _positive_int(getattr(self, name), name)
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError('seed must be in [0, 2**32)')
        if self.n_paths > 100_000 or self.n_steps > 100_000 or self.batch_size > 256:
            raise ValueError('plan exceeds supported path, horizon or batch bounds')

    def check_memory(self, assets):
        # Includes output and conservative per-path return/shock/index workspace.
        required = 8 * min(self.n_paths, self.batch_size) * (self.n_steps + 1) * (4 * assets + 4)
        if required > self.max_batch_bytes:
            raise ValueError('batch workspace exceeds max_batch_bytes')


def _prices(initial, increments):
    with np.errstate(over='raise', under='raise', invalid='raise'):
        try:
            result = np.empty((len(increments) + 1, len(initial)))
            result[0] = initial
            result[1:] = np.exp(np.log(initial) + np.cumsum(increments, axis=0))
        except FloatingPointError as exc:
            raise ValueError('scenario price exceeds finite positive float64 range') from exc
    if not np.isfinite(result).all() or (result <= 0).any():
        raise ValueError('scenario prices must be finite and positive')
    return result


class _Generator:
    def batches(self, plan=PathPlan()):
        """Yield (first_path_id, prices); batch layout does not change a path's RNG."""
        plan.check_memory(len(self.assets))
        for begin in range(0, plan.n_paths, plan.batch_size):
            count = min(plan.batch_size, plan.n_paths - begin)
            paths = np.empty((count, plan.n_steps + 1, len(self.assets)))
            for i in range(count):
                rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([plan.seed, begin + i])))
                paths[i] = self._path(rng, plan.n_steps)
            yield begin, paths

    def path_evidence(self, plan, path_id):
        """Replay a path's latent events without changing its sampling stream."""
        rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([plan.seed, path_id])))
        evidence = []
        self._path(rng, plan.n_steps, evidence=evidence)
        return evidence

    def receipt(self, plan=PathPlan()):
        plan.check_memory(len(self.assets))
        return dict(model=self.model, model_version=VERSION, parameters=json.loads(self._parameters),
            input=json.loads(self._input), plan=asdict(plan), numpy_version=np.__version__,
            rng='PCG64 / SeedSequence([seed, global_path_id]); one stream per path',
            output='SIMULATED_CLOSE_ONLY_PRICES; not historical OHLCV or future market observations',
            qualification='SYNTHETIC_RISK_SCENARIO; NOT_MARKET_STATISTICALLY_VALIDATED',
            calibration=json.loads(self._calibration))


@dataclass(frozen=True)
class RegimeConfig:
    block_size: int = 24
    lookback: int = 24
    reference_asset: str = 'BINANCE:USDM:NVDAUSDT'
    volatility_quantile: float = .5
    min_blocks_per_state: int = 20
    min_transitions_per_state: int = 20

    def __post_init__(self):
        for name in ('block_size', 'lookback', 'min_blocks_per_state', 'min_transitions_per_state'):
            _positive_int(getattr(self, name), name)
        if self.lookback < 2 or not 0 < self.volatility_quantile < 1:
            raise ValueError('lookback >= 2 and volatility_quantile in (0, 1) required')


class RegimeBootstrap(_Generator):
    """Frozen prefix thresholds; suffix pools with lagged features and joint rows.

    States are (sample log volatility > threshold, sum of log returns >= 0).
    Transitions are estimated at exactly block_size observations, not one step.
    """
    model = 'state_conditional_block_bootstrap'

    def __init__(self, *, returns, timestamps, available_at, as_of, threshold_as_of,
                 assets, initial_prices, source, config=RegimeConfig(),
                 transition=None, initial_state=None):
        self.assets = tuple(assets)
        n = len(self.assets)
        if (not n or len(set(self.assets)) != n
                or any(not isinstance(a, str) or not a for a in self.assets)):
            raise ValueError('assets must be unique nonempty names')
        if config.reference_asset not in self.assets:
            raise ValueError('reference_asset must identify a supplied asset')
        self.initial_prices = _readonly(_vector(initial_prices, n, 'initial_prices'))
        if (self.initial_prices <= 0).any():
            raise ValueError('initial_prices must be positive')
        data = np.array(returns, dtype=float, copy=True)
        if (data.ndim != 2 or data.shape[1] != n or not np.isfinite(data).all()
                or (data <= -1).any()):
            raise ValueError('returns must be complete finite simple returns > -1')
        times = pd.DatetimeIndex([_time(t) for t in timestamps])
        availability = pd.DatetimeIndex([_time(t) for t in available_at])
        cutoff, threshold_cutoff = _time(as_of), _time(threshold_as_of)
        if len(times) != len(data) or len(availability) != len(data) or len(data) < 2:
            raise ValueError('incomplete timestamped history; at least two rows required')
        delta = np.diff(times.asi8)
        if (delta <= 0).any() or not (delta == delta[0]).all():
            raise ValueError('history must have strictly increasing, equally spaced synchronized observations')
        if (availability < times).any() or (times > cutoff).any() or (availability > cutoff).any():
            raise ValueError('history must be observed and available no later than as_of')
        if threshold_cutoff >= cutoff:
            raise ValueError('threshold_as_of must precede calibration as_of')
        split = int(times.searchsorted(threshold_cutoff, side='right'))
        if split < config.lookback + 1 or len(data) - split < config.block_size:
            raise CalibrationError('insufficient prefix for thresholds or suffix for complete blocks')
        if not isinstance(source, dict) or not source.get('source') or not source.get('data_version'):
            raise ValueError('source and data_version are required provenance')
        # The prefix is a statistical observation-time split. Actual acquisition
        # availability remains the calibration cutoff, not the prefix boundary.
        logs = np.log1p(data[:, self.assets.index(config.reference_asset)])
        windows = np.lib.stride_tricks.sliding_window_view(logs, config.lookback)
        vol = windows.std(axis=1, ddof=1)
        trend = windows.sum(axis=1)
        threshold = float(np.quantile(vol[:split - config.lookback + 1], config.volatility_quantile))
        starts = np.arange(split, len(data) - config.block_size + 1)
        labels = ((vol[starts - config.lookback] > threshold).astype(int) * 2
                  + (trend[starts - config.lookback] >= 0).astype(int))
        pools = tuple(starts[labels == state] for state in range(4))
        counts = np.asarray([len(pool) for pool in pools])
        transitions = np.zeros((4, 4), dtype=int)
        # Use every eligible overlapping pair, always separated by one full block.
        if len(labels) > config.block_size:
            np.add.at(transitions, (labels[:-config.block_size], labels[config.block_size:]), 1)
        if (counts < config.min_blocks_per_state).any():
            raise CalibrationError(f'insufficient conditional block support: {dict(zip(STATES, counts.tolist()))}')
        if transition is None:
            if (transitions.sum(axis=1) < config.min_transitions_per_state).any():
                raise CalibrationError('insufficient transition support; use an explicit parameterized stress matrix')
            matrix = transitions / transitions.sum(axis=1, keepdims=True)
            mode = 'HISTORICAL_BLOCK_LAG_ESTIMATE; overlapping pairs, not independent observations'
        else:
            matrix = _probabilities(transition, (4, 4), 'transition')
            mode = 'PARAMETERIZED_STRESS_ASSUMPTION; not estimated market probabilities'
        initial = counts / counts.sum() if initial_state is None else _probabilities(initial_state, (4,), 'initial_state')
        self._returns = _readonly(data)
        self._pools = tuple(_readonly(pool) for pool in pools)
        self._transition = _readonly(matrix)
        self._initial = _readonly(initial)
        self._block_size = config.block_size
        self._parameters = json.dumps(dict(config=asdict(config), transition=matrix.tolist(),
            initial_state=initial.tolist(), initial_state_source='pool frequencies' if initial_state is None else 'explicit stress assumption',
            transition_source=mode, states=STATES, initial_prices=self.initial_prices.tolist()), allow_nan=False)
        identity = dict(source=source, assets=self.assets, timestamps=[t.isoformat() for t in times],
            available_at=[t.isoformat() for t in availability], as_of=cutoff.isoformat())
        digest = hashlib.sha256(bytes.fromhex(json_hash(identity)))
        digest.update(np.ascontiguousarray(data, dtype='<f8').tobytes())
        self._input = json.dumps(dict(sample_hash=digest.hexdigest(), source=source,
            assets=self.assets, observations=[times[0].isoformat(), times[-1].isoformat()],
            count=len(data), cadence_ns=int(delta[0]), as_of=cutoff.isoformat(),
            availability_max=availability.max().isoformat()), allow_nan=False)
        self._calibration = json.dumps(dict(as_of=cutoff.isoformat(), threshold_observation_cutoff=threshold_cutoff.isoformat(),
            threshold_available_at=max(availability[:split]).isoformat(), volatility_threshold=threshold,
            prefix_rows=split, suffix_first_block=int(starts[0]), block_counts=counts.tolist(),
            transition_counts=transitions.tolist(), transition_lag_rows=config.block_size,
            minimum_support='engineering thresholds, not evidence of statistical adequacy',
            features='strictly preceding lookback log returns; no block returns in its label',
            historical_realtime_availability_claim=False), allow_nan=False)

    def _path(self, rng, steps, evidence=None):
        state = rng.choice(4, p=self._initial)
        sampled = np.empty((steps, len(self.assets)))
        for begin in range(0, steps, self._block_size):
            start = rng.choice(self._pools[state])
            size = min(self._block_size, steps - begin)
            sampled[begin:begin + size] = self._returns[start:start + size]
            if evidence is not None:
                evidence.append(dict(begin=int(begin), size=int(size), state=STATES[state],
                    historical_start=int(start)))
            if begin + size < steps:
                state = rng.choice(4, p=self._transition[state])
        return _prices(self.initial_prices, np.log1p(sampled))


@dataclass(frozen=True)
class JumpConfig:
    """Per-step Bernoulli common event with deterministic signed log jump loads."""
    log_drift: tuple = (0.,)
    volatility: tuple = (.004,)
    jump_probability: float = .02
    jump_log_sizes: tuple = (-.25,)

    def __post_init__(self):
        if not np.isfinite(self.jump_probability) or not 0 <= self.jump_probability <= 1:
            raise ValueError('jump_probability must be in [0, 1]')
        n = len(self.log_drift)
        if not n:
            raise ValueError('at least one asset required')
        for name in ('log_drift', 'volatility', 'jump_log_sizes'):
            _vector(getattr(self, name), n, name)
        if (np.asarray(self.volatility) < 0).any():
            raise ValueError('volatility must be nonnegative')


class JumpStress(_Generator):
    model = 'parameterized_jump_stress'

    def __init__(self, *, assets, initial_prices, as_of, source, config=JumpConfig()):
        self.assets = tuple(assets)
        n = len(self.assets)
        if not n or len(set(self.assets)) != n or any(not isinstance(a, str) or not a for a in self.assets):
            raise ValueError('assets must be unique nonempty names')
        self.initial_prices = _readonly(_vector(initial_prices, n, 'initial_prices'))
        if (self.initial_prices <= 0).any() or len(config.log_drift) != n:
            raise ValueError('positive initial prices and matching parameter asset dimensions required')
        if not isinstance(source, dict) or not source.get('source') or not source.get('data_version'):
            raise ValueError('source and data_version are required provenance')
        self._drift = _readonly(config.log_drift)
        self._vol = _readonly(config.volatility)
        self._jump = _readonly(config.jump_log_sizes)
        self._probability = config.jump_probability
        self._parameters = json.dumps(dict(**asdict(config), initial_prices=self.initial_prices.tolist(),
            event='one Bernoulli per time step, shared across assets; at most one event per step',
            jump_distribution='point mass at signed per-asset log sizes',
            background='independent asset Gaussian log increments, no jump compensation; dt=1 step'), allow_nan=False)
        identity = dict(assets=self.assets, source=source, as_of=_time(as_of).isoformat())
        self._input = json.dumps(dict(**identity, sample_hash=json_hash(identity)), allow_nan=False)
        self._calibration = json.dumps(dict(as_of=_time(as_of).isoformat(),
            mode='PARAMETERIZED_STRESS_ONLY; all event frequencies and sizes are assumptions',
            market_probability_validated=False))

    def _path(self, rng, steps, evidence=None):
        background = rng.normal(size=(steps, len(self.assets))) * self._vol + self._drift
        events = rng.random(steps) < self._probability
        if evidence is not None:
            evidence.extend(dict(step=int(i + 1), jump_log_sizes=self._jump.tolist(),
                total_simple_return=np.expm1(background[i] + self._jump).tolist())
                for i in np.flatnonzero(events))
        return _prices(self.initial_prices, background + events[:, None] * self._jump)
