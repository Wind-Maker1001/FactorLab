"""Independent static-grid research simulation with an authoritative CPU oracle."""
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from time import perf_counter

import numpy as np

from factorlab.research.monte_carlo import _loss_tail
from factorlab.strategies.grid import GridConfig, GridStrategy
from factorlab.strategies.grid_execution import GridRules
from factorlab.experiments.grid_funding_adaptive import FundingPolicy, VolatilityPolicy
from .inputs import BatchMarketInputs

PRICE_SCALE = 1_000_000
STATES = ('RUNNING', 'PAUSED', 'EXITING', 'EXITED', 'WARMUP')
REASONS = (None, 'BREAKOUT', 'MAX_DRAWDOWN', 'MAX_TURNOVER',
           'BREAKOUT_REBUILD_INTERVAL', 'INVALID_RANGE', 'FUNDING_SPIKE_EXIT',
           'FUNDING_EXTREME_EXIT', 'FUNDING_AGE_EXIT', 'FUNDING_MARGIN_EXHAUSTED')
COLUMNS = ('cash', 'inventory', 'available', 'committed', 'realized_gross',
           'unrealized', 'equity', 'fee', 'slippage', 'drawdown', 'fills', 'state', 'reason',
           'total_fees', 'total_slippage', 'maximum_drawdown',
           'funding_pnl', 'total_funding_pnl', 'rebuilds', 'center', 'spacing',
           'volatility', 'buy_blocked', 'pending_rebuild', 'last_rebuild', 'funding_bps_h')


class UnsupportedGridSimulation(ValueError):
    """Requested economics cannot be represented by this research kernel."""


def _integer(value, scale, name):
    scaled = Decimal(str(value)) * scale
    if scaled != scaled.to_integral_value():
        raise UnsupportedGridSimulation(f'{name} must be exactly representable at scale {scale}')
    return int(scaled)


@dataclass(frozen=True)
class BatchGridConfig:
    grid: GridConfig
    rules: GridRules
    capital: Decimal = Decimal('1000')
    fee_rate: Decimal = Decimal('.0004')
    slippage: Decimal = Decimal('.0006')
    capacity: Decimal = Decimal('.10')
    funding_policy: FundingPolicy | None = None
    volatility_policy: VolatilityPolicy | None = None

    @property
    def workspace_width(self):
        return max(self.grid.atr_window if self.grid.mode == 'atr' else 0,
                   self.volatility_policy.vol_window_hours if self.volatility_policy else 0, 1) + 1

    def __post_init__(self):
        g, r = self.grid, self.rules
        if g.mode not in ('arithmetic', 'geometric', 'atr') or g.breakout not in ('pause', 'exit', 'rebuild'):
            raise UnsupportedGridSimulation('unsupported grid mode or breakout policy')
        if self.funding_policy is not None and not isinstance(self.funding_policy, FundingPolicy):
            raise TypeError('funding_policy requires FundingPolicy')
        if self.volatility_policy is not None and not isinstance(self.volatility_policy, VolatilityPolicy):
            raise TypeError('volatility_policy requires VolatilityPolicy')
        if self.volatility_policy is not None and (self.funding_policy is not None or g.mode == 'atr' or g.levels != 1):
            raise UnsupportedGridSimulation('C requires one level and cannot combine with ATR or B')
        if self.funding_policy is not None and g.mode == 'atr':
            raise UnsupportedGridSimulation('B and ATR are distinct profiles and cannot combine')
        if self.funding_policy is not None:
            b = self.funding_policy
            if type(b.lookback_hours) is not int or not 1 <= b.lookback_hours <= 4096 or type(b.conservative) is not bool:
                raise UnsupportedGridSimulation('B requires an integer lookback in [1, 4096] and boolean conservative')
            if b.expected_hold_hours < 0 or any(value is not None and value < 0 for value in (b.exit_bps_h, b.max_hold_hours)):
                raise UnsupportedGridSimulation('B hold periods and optional exit thresholds must be nonnegative')
        if self.volatility_policy is not None:
            c = self.volatility_policy
            if (type(c.vol_window_hours) is not int or not 2 <= c.vol_window_hours <= 4096
                    or type(c.min_rebuild_interval) is not int or c.min_rebuild_interval < 0
                    or type(c.allow_flat_rebuild) is not bool):
                raise UnsupportedGridSimulation('C requires bounded integer window/interval and boolean allow_flat_rebuild')
            if (c.k_mult <= 0 or c.horizon_hours <= 0 or c.rebuild_width_delta < 0
                    or (c.extreme_vol_mult is not None and c.extreme_vol_mult < 0)):
                raise UnsupportedGridSimulation('C requires positive width scale/horizon and nonnegative thresholds')
        if self.workspace_width > 4097:
            raise UnsupportedGridSimulation('history windows cannot exceed 4096 observations')
        for policy in (self.funding_policy, self.volatility_policy):
            if policy is not None:
                for name, value in vars(policy).items():
                    if isinstance(value, (int, float)) and not np.isfinite(value):
                        raise ValueError(f'policy {name} must be finite')
        if g.center_rule != 'last_close':
            raise UnsupportedGridSimulation('only last_close center rule supported')
        if g.budget is not None or g.expiry_reseed_limit:
            raise UnsupportedGridSimulation('budget sizing and expiry reseeding are not supported')
        if not 1 <= g.levels <= 32:
            raise UnsupportedGridSimulation('levels must be in [1, 32]')
        for name in ('capital', 'fee_rate', 'slippage', 'capacity'):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f'{name} requires a finite nonnegative Decimal')
        if self.capital <= 0 or self.capacity <= 0 or self.fee_rate >= 1 or self.slippage >= 1:
            raise ValueError('positive capital/capacity and rates below one required')
        for name, value in [('initial_price', g.initial_price), ('tick_size', r.tick_size),
                            ('fee_rate', self.fee_rate), ('slippage', self.slippage)]:
            _integer(value, PRICE_SCALE, name)
        for name, value in [('quantity', Decimal(g.quantity) * r.quantity_unit),
                            ('initial_inventory', Decimal(g.initial_inventory) * r.quantity_unit),
                            ('max_inventory', Decimal(g.max_inventory) * r.quantity_unit)]:
            _integer(value / r.step_size, 1, name)
        if r.step_size > 1 or self.capital > 1_000_000 or g.upper > 1_000_000:
            raise UnsupportedGridSimulation('quantity step <=1, capital/upper <=1000000 required')
        if Decimal(g.initial_inventory) * r.quantity_unit * Decimal(str(g.initial_price)) > self.capital:
            raise UnsupportedGridSimulation('initial inventory collateral exceeds capital')
        _integer(r.step_size, 100_000_000, 'step_size')
        _integer(r.min_notional, 100_000_000, 'min_notional')
        if (Decimal(g.max_inventory) * r.quantity_unit > 1_000_000
                or Decimal(g.max_inventory) * r.quantity_unit / r.step_size > 2_000_000_000
                or r.min_notional > 1_000_000):
            raise UnsupportedGridSimulation('inventory or minimum notional exceeds numeric range')
        if self.capacity / r.step_size > 10000:
            raise UnsupportedGridSimulation('capacity exceeds 10000 quantity steps')

    @lru_cache(maxsize=32)
    def packed(self, steps):
        from decimal import ROUND_CEILING, ROUND_FLOOR
        g, r = self.grid, self.rules
        strategy = GridStrategy(g)
        targets = np.asarray(strategy.prices, dtype=np.float64)
        limits = np.asarray([[
            int((Decimal(str(p)) / r.tick_size).to_integral_value(rounding=rounding)
                * r.tick_size * PRICE_SCALE) for p in targets]
            for rounding in (ROUND_FLOOR, ROUND_CEILING)], dtype=np.int64)
        minimum = max(r.min_quantity, Decimal(g.minimum_quantity) * r.quantity_unit)
        params = np.asarray([float(self.capital), float(r.step_size), float(r.min_notional),
            float(self.fee_rate), g.slippage_bps / 10000, g.max_cash_committed,
            g.max_drawdown, g.max_turnover, g.minimum_price,
            128 * np.finfo(float).eps * (steps + 128) * max(float(self.capital), g.max_cash_committed)], dtype=np.float64)
        integers = np.asarray([g.levels + 1, g.order_ttl, int(g.breakout == 'exit'),
            int(Decimal(g.quantity) * r.quantity_unit / r.step_size),
            int(Decimal(g.initial_inventory) * r.quantity_unit / r.step_size),
            int(Decimal(g.max_inventory) * r.quantity_unit / r.step_size),
            int(minimum.__truediv__(r.step_size).to_integral_value(rounding=ROUND_CEILING)),
            int(self.capacity / r.step_size), _integer(self.slippage, PRICE_SCALE, 'slippage'),
            _integer(r.step_size, 100_000_000, 'step_size'),
            _integer(r.min_notional, 100_000_000, 'min_notional')], dtype=np.int64)
        b = self.funding_policy or FundingPolicy()
        c = self.volatility_policy or VolatilityPolicy()
        params = np.concatenate((params, np.asarray([
            g.atr_multiplier, g.rebuild_threshold, float(r.tick_size), c.k_mult,
            c.horizon_hours, c.min_half_width, c.max_half_width,
            -1 if c.extreme_vol_mult is None else c.extreme_vol_mult,
            c.rebuild_width_delta, c.reference_vol_1h, b.entry_max_bps_h,
            b.expected_hold_hours, b.min_net_margin_bps,
            -1 if b.exit_bps_h is None else b.exit_bps_h,
            -1 if b.max_hold_hours is None else b.max_hold_hours,
            g.upper / g.lower, g.upper - g.lower, g.fee_bps / 10000], dtype=np.float64)))
        integers = np.concatenate((integers, np.asarray([
            1 if self.volatility_policy else 2 if self.funding_policy else 0,
            ('arithmetic', 'geometric', 'atr').index(g.mode), int(g.breakout == 'rebuild'),
            g.atr_window, g.min_rebuild_interval, c.vol_window_hours,
            int(c.allow_flat_rebuild), c.min_rebuild_interval, b.lookback_hours,
            int(b.conservative), ('none', 'funding_bleed', 'net_margin').index(b.exit_mode),
            0], dtype=np.int64)))
        return targets, limits, params, integers


@dataclass
class BatchGridResult:
    terminal: np.ndarray
    trace: np.ndarray | None
    metadata: dict
    fills: list | None = None
    events: list | None = None

    def risk(self, capital, confidence=.95):
        nav = self.terminal[:, COLUMNS.index('equity')]
        var, cvar = _loss_tail(1 - nav / float(capital), confidence)
        return dict(paths=len(nav), confidence=confidence, var=var, cvar=cvar,
                    loss_probability=float(np.mean(nav < float(capital))))


def freeze_prices(prices):
    """Explicit six-decimal quote preparation; freeze this output for both engines."""
    values = np.asarray(prices, dtype=np.float64)
    return np.round(values, 6)


def _quotes(prices, config):
    values = np.asarray(prices, dtype=np.float64)
    if values.ndim != 2 or min(values.shape) < 1 or not np.isfinite(values).all():
        raise ValueError('nonempty finite [path, observation] prices required')
    if values.shape[1] > 4097:
        raise UnsupportedGridSimulation('at most 4096 sequential price increments supported')
    if np.any(values <= 0) or np.any(values > 1_000_000):
        raise UnsupportedGridSimulation('quotes must be in (0, 1000000]')
    scaled = np.ascontiguousarray(np.rint(values * PRICE_SCALE), dtype=np.int64)
    if not np.array_equal(scaled.astype(float) / PRICE_SCALE, values):
        raise UnsupportedGridSimulation('quotes require <=6 decimal places; freeze explicitly before both engines')
    if np.any(values[:, 0] != config.grid.initial_price):
        raise UnsupportedGridSimulation('first quote must equal endowed initial price')
    return values, scaled


@lru_cache(maxsize=1)
def _cuda_kernel():
    import cupy as cp
    source = Path(__file__).with_name('kernel.cu').read_text()
    kernel = cp.RawKernel(source, 'grid_batch', options=('--std=c++17', '--fmad=false'))
    kernel.compile()
    return kernel


def simulate(prices, config, *, backend='cpu', trace=False, batch_size=4096, market=None,
             collect_fills=True):
    """Run frozen quotes; collect_fills=False retains traces/events but omits fill exports."""
    values, scaled = _quotes(prices, config)
    extended = market is not None or config.grid.mode == 'atr' or config.grid.breakout == 'rebuild' or config.funding_policy is not None or config.volatility_policy is not None
    if market is None:
        if config.grid.mode == 'atr' or config.funding_policy is not None or config.volatility_policy is not None:
            raise ValueError('dynamic profiles require explicit BatchMarketInputs')
        market = BatchMarketInputs(np.arange(values.shape[1], dtype=np.int64))
    if not isinstance(market, BatchMarketInputs):
        raise TypeError('market requires BatchMarketInputs')
    prepared = market.prepare(values, config)
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('positive batch_size required')
    if trace:
        batch_size = min(batch_size, max(1, (64 * 1024 * 1024) // (values.shape[1] * (128 * 8 + 512 * 11 + len(COLUMNS)) * 8)))
    if backend == 'paper':
        if extended:
            from .paper import run_paper_reference
            return run_paper_reference(values, config, prepared, trace, collect_fills)
        return _reference(values, config, trace, collect_fills)
    if backend == 'cpu':
        return _native(scaled, config, trace, batch_size, prepared, collect_fills)
    if backend != 'cuda':
        raise ValueError('backend must be paper, cpu or cuda')
    setup_started = perf_counter()
    import cupy as cp
    targets, limits, params, integers = config.packed(values.shape[1])
    integers = integers.copy()
    integers[22] = int(bool(len(prepared.funding_us)))
    compile_started = perf_counter()
    kernel = _cuda_kernel()
    compile_seconds = perf_counter() - compile_started
    device_args = tuple(cp.asarray(x) for x in (targets, limits, params, integers))
    cp.cuda.get_current_stream().synchronize()
    setup_seconds = perf_counter() - setup_started
    terminal = np.empty((len(values), len(COLUMNS)), dtype=np.float64)
    traces = np.empty((*values.shape, len(COLUMNS)), dtype=np.float64) if trace else None
    fill_rows = [] if trace and collect_fills else None
    event_rows = [] if trace else None
    device_seconds = transfer_seconds = upload_seconds = 0.
    started = perf_counter()
    peak = 0
    for begin in range(0, len(values), batch_size):
        end = min(len(values), begin + batch_size)
        upload_started = perf_counter()
        d_prices = cp.asarray(scaled[begin:end])
        output = cp.empty((end - begin, values.shape[1] if trace else 1, len(COLUMNS)), dtype=cp.float64)
        fills = cp.zeros((end-begin, values.shape[1], 128, 8) if trace else (1,), dtype=cp.float64)
        errors = cp.zeros(end - begin, dtype=cp.int32)
        audit = cp.zeros((end-begin, values.shape[1], 512, 11) if trace else (1,), dtype=cp.float64)
        d_market = tuple(cp.asarray(x) for x in prepared.batch(begin, end))
        workspace = cp.empty((end-begin, config.workspace_width), dtype=cp.float64)
        cp.cuda.get_current_stream().synchronize()
        upload_seconds += perf_counter() - upload_started
        before = perf_counter()
        kernel(((end - begin + 127) // 128,), (128,),
               (d_prices, np.int32(end - begin), np.int32(values.shape[1]), *device_args,
                np.int32(trace), output, fills, errors, *d_market, workspace,
                np.int32(len(prepared.funding_us)), np.int32(prepared.warmup.shape[1]),
                np.int32(config.workspace_width), audit))
        cp.cuda.get_current_stream().synchronize()
        device_seconds += perf_counter() - before
        peak = max(peak, cp.get_default_memory_pool().used_bytes())
        failures = cp.asnumpy(errors)
        if np.any(failures):
            path = begin + int(np.flatnonzero(failures)[0])
            raise UnsupportedGridSimulation(f'CUDA path {path}: unsupported runtime condition {failures[path-begin]}')
        before = perf_counter()
        host = cp.asnumpy(output)
        terminal[begin:end] = host[:, -1]
        if trace:
            traces[begin:end] = host
        if trace and collect_fills:
            for path_fills in cp.asnumpy(fills):
                fill_rows.append(np.asarray([[step, *row] for step, rows in enumerate(path_fills)
                                             for row in rows if row[0]], dtype=float).reshape(-1, 9))
        if trace:
            event_rows.extend(_audit_rows(cp.asnumpy(audit)))
        transfer_seconds += perf_counter() - before
        del d_prices, output, fills, errors, audit, d_market, workspace
    return BatchGridResult(terminal, traces, dict(backend='cuda', evidence='RESEARCH_SIMULATION',
        seconds=perf_counter() - started, kernel_seconds=device_seconds,
        setup_seconds=setup_seconds, compile_or_cache_seconds=compile_seconds,
        upload_and_allocation_seconds=upload_seconds,
        output_transfer_seconds=transfer_seconds, device_allocation_peak_bytes=peak,
        device=cp.cuda.runtime.getDeviceProperties(cp.cuda.Device().id)['name'].decode(),
        cupy=cp.__version__, quote_decimals=6), fill_rows, event_rows)


def _audit_rows(audit):
    return [np.asarray([[step, *row] for step, rows in enumerate(path)
                        for row in rows if row[0]], dtype=float).reshape(-1, 12)
            for path in audit]


def _native(quotes, config, trace, batch_size, prepared, collect_fills):
    from .native import run, _library
    before = perf_counter()
    _library()
    setup = perf_counter() - before
    started = perf_counter()
    targets, limits, params, integers = config.packed(quotes.shape[1])
    integers = integers.copy()
    integers[22] = int(bool(len(prepared.funding_us)))
    args = targets, limits, params, integers
    terminal = np.empty((len(quotes), len(COLUMNS)))
    traces = np.empty((*quotes.shape, len(COLUMNS))) if trace else None
    fill_rows = [] if trace and collect_fills else None
    event_rows = [] if trace else None
    compute_seconds = 0.
    for begin in range(0, len(quotes), batch_size):
        end = min(len(quotes), begin + batch_size)
        output, fills, errors, seconds, audit = run(quotes[begin:end], *args, trace,
                                                   prepared.batch(begin, end), config.workspace_width)
        compute_seconds += seconds
        if np.any(errors):
            path = begin + int(np.flatnonzero(errors)[0])
            raise UnsupportedGridSimulation(f'CPU path {path}: unsupported runtime condition {errors[path-begin]}')
        terminal[begin:end] = output[:, -1]
        if trace:
            traces[begin:end] = output
        if trace and collect_fills:
            for path_fills in fills:
                fill_rows.append(np.asarray([[step, *row] for step, rows in enumerate(path_fills)
                                             for row in rows if row[0]], dtype=float).reshape(-1, 9))
        if trace:
            event_rows.extend(_audit_rows(audit))
    return BatchGridResult(terminal, traces, dict(backend='cpu', evidence='RESEARCH_SIMULATION',
        seconds=perf_counter()-started, setup_seconds=setup, kernel_seconds=compute_seconds,
        quote_decimals=6), fill_rows, event_rows)


def _reference(values, config, trace, collect_fills):
    from factorlab.experiments.grid_paper import run_experiment
    from factorlab.experiments.grid_monte_carlo import close_bars
    from factorlab.trading.contracts import Market
    started = perf_counter()
    terminal = np.empty((len(values), len(COLUMNS)))
    traces = np.empty((*values.shape, len(COLUMNS))) if trace else None
    fill_rows = [] if trace and collect_fills else None
    for index, prices in enumerate(values):
        _, artifacts = run_experiment(None, bars=close_bars(prices, '2026-11-15T00:00:00Z'),
            config=config.grid, rules=config.rules, capital=config.capital,
            fee_rate=config.fee_rate, slippage=config.slippage, interval='1h',
            market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT',
            scenario='CLOSE_ONLY_SCENARIO', synthetic_capacity=config.capacity,
            in_memory_store=True, return_artifacts=True)
        rows = artifacts['rows']
        fills_by_time = {}
        for trade in artifacts['trades']:
            fills_by_time[trade['execution_date']] = fills_by_time.get(trade['execution_date'], 0) + 1
        total = 0
        fees = slip = drawdown = 0.
        for step, row in enumerate(rows):
            total += fills_by_time.get(row['date'], 0)
            fees += row['fee']
            slip += row['slippage']
            drawdown = max(drawdown, row['drawdown'])
            extras = dict(fills=total, state=STATES.index(row['state']),
                          reason=REASONS.index(row['state_reason']),
                          total_fees=fees, total_slippage=slip, maximum_drawdown=drawdown,
                          funding_pnl=0., total_funding_pnl=0., rebuilds=0.,
                          center=config.grid.initial_price,
                          spacing=(config.grid.upper-config.grid.lower)/config.grid.levels,
                          volatility=0., buy_blocked=0., pending_rebuild=0.,
                          last_rebuild=0., funding_bps_h=0.)
            data = [extras[k] if k in extras else row[k] for k in COLUMNS]
            if trace:
                traces[index, step] = data
        terminal[index] = data
        if trace and collect_fills:
            steps = {row['date']: i for i, row in enumerate(rows)}
            orders = artifacts['result']['orders']
            fill_rows.append(np.asarray([[steps[t['execution_date']], int(t['order_id'].rsplit('-', 1)[1]),
                int(t['side'] == 'SELL'), t['grid_level'], t['executed_shares'],
                t['execution_price'], t['fee'], t['slippage'],
                int(orders[t['order_id']]['purpose'] == 'RISK_EXIT')]
                for t in artifacts['trades']], dtype=float).reshape(-1, 9))
    return BatchGridResult(terminal, traces, dict(backend='cpu', evidence='AUTHORITATIVE_PAPER_IN_MEMORY',
        seconds=perf_counter() - started, quote_decimals=6), fill_rows)
