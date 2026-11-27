"""Chronological historical relative replays and persisted Paper/FIFO audits."""
from dataclasses import asdict, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.experiments.grid_empirical import load_hourly
from factorlab.experiments.grid_fifo import attribution
from factorlab.experiments.grid_monte_carlo import close_bars
from factorlab.experiments.grid_paper import _write, run_experiment
from factorlab.research.grid_simulation import COLUMNS, freeze_prices, simulate
from factorlab.trading.contracts import Market


CERTIFIED_BARS_SHA256 = '16aa66258880efdace966880f59691a29cb74f4f87428240a5ef247ad044a2ab'
PROVENANCE = ('Task-heldout history after calibration ends at the 2026-07-15T00:00:00Z close endpoint; '
              'the old frozen baseline and prior research used the full snapshot, so this is not '
              'historically untouched out-of-sample evidence. The full-calibration pilot was runtime-only, '
              'not selection. '
              'Relative close-only replay; no funding; synthetic capacity, not observed liquidity.')


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def freeze_windows(snapshot, start='2026-07-15T00:00:00Z',
                   end='2026-08-12T00:00:00Z', steps=168):
    """Freeze half-open hourly windows anchored by the preceding bar's close.

    Close returns in a window become known only at the end of their bars. State
    uses at most the preceding 168 completed closes, never the replay outcomes.
    The final short window is retained and its actual horizon is explicit.
    """
    if type(steps) is not int or not 1 <= steps <= 4096:
        raise ValueError('steps must be an integer in [1, 4096]')
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    if begin.tzinfo is None or finish.tzinfo is None or begin >= finish:
        raise ValueError('ordered timezone-aware boundaries required')
    if begin != begin.floor('h') or finish != finish.floor('h'):
        raise ValueError('hour-aligned boundaries required')
    bars, identity = load_hourly(snapshot)
    if identity['bars_sha256'] != CERTIFIED_BARS_SHA256:
        raise ValueError('certified May14-Aug12 snapshot required')
    times = pd.DatetimeIndex([b['time'] for b in bars])
    selected = np.flatnonzero((times >= begin) & (times < finish))
    expected = pd.date_range(begin, finish, freq='h', inclusive='left')
    if list(times[selected]) != list(expected):
        raise ValueError('snapshot does not cover requested validation interval')
    if selected[0] == 0:
        raise ValueError('preceding completed bar required for start anchor')
    windows = []
    closes = np.asarray([b['close'] for b in bars], dtype=float)
    for offset in range(0, len(selected), steps):
        indices = selected[offset:offset + steps]
        first = int(indices[0])
        known = closes[max(0, first - 169):first]
        returns = np.diff(np.log(known))
        state = dict(known_returns=len(returns),
                     trend_log_return=float(returns.sum()) if len(returns) else None,
                     hourly_volatility=float(returns.std()) if len(returns) else None,
                     known_before=times[first].isoformat())
        raw = [float(closes[first - 1]), *closes[indices].tolist()]
        window = dict(start=times[first].isoformat(),
                      end=(times[indices[-1]] + pd.Timedelta(hours=1)).isoformat(),
                      steps=len(indices), prices=raw, market_state=state)
        window['id'] = _identity(window)
        windows.append(window)
    manifest = dict(study='MC-GRID-OPT-01', identity=identity, start=begin.isoformat(),
                    end=finish.isoformat(), steps=steps, windows=windows,
                    anchoring='preceding bar close at start endpoint, followed by completed hourly closes; rescale each path to grid.initial_price then freeze six decimals',
                    return_boundary='bar times are opens; calibration close endpoints <=start; heldout bars open >=start, first heldout close endpoint=start+1h',
                    calibration_end_close_endpoint=begin.isoformat(),
                    provenance_warning=PROVENANCE)
    manifest['id'] = _identity(manifest)
    return manifest


def _metrics(result, prices, config):
    columns = {name: result.trace[:, :, i] for i, name in enumerate(COLUMNS)}
    return [dict(net_return=float(columns['equity'][i, -1] / float(config.capital) - 1),
                 maximum_drawdown=float(columns['maximum_drawdown'][i, -1]),
                 fills=int(columns['fills'][i, -1]),
                 maximum_committed=float(columns['committed'][i].max()),
                 maximum_inventory=float(columns['inventory'][i].max()),
                 terminal_inventory=float(columns['inventory'][i, -1]),
                 fees=float(columns['total_fees'][i, -1]),
                 slippage=float(columns['total_slippage'][i, -1]),
                 turnover=float(sum(row[4] * row[5] for row in result.fills[i])))
            for i in range(len(prices))]


def replay_windows(manifest, configs, backend='cpu'):
    """Evaluate (id, BatchGridConfig) pairs under crossed cost/capacity stresses."""
    from factorlab.experiments.grid_parameter_study import METRICS, passive_paths, reduce_paths
    rows = []
    for key, config in configs:
        for cost in (1, 2):
            for capacity in (Decimal('1'), Decimal('.5')):
                settings = replace(config,
                    grid=replace(config.grid, fee_bps=config.grid.fee_bps * cost,
                                 slippage_bps=config.grid.slippage_bps * cost),
                    fee_rate=config.fee_rate * cost, slippage=config.slippage * cost,
                    capacity=config.capacity * capacity)
                for window in manifest['windows']:
                    raw = np.asarray(window['prices'])
                    prices = freeze_prices((raw / raw[0] * settings.grid.initial_price)[None, :])
                    result = simulate(prices, settings, backend=backend, trace=True)
                    metrics = dict(zip(METRICS, reduce_paths(result, prices, settings)[0].tolist()))
                    controls = {name: dict(zip(METRICS, value[0].tolist())) for name, value in (
                        ('cash', np.zeros((1, len(METRICS)))),
                        ('hold', passive_paths(prices, settings)),
                        ('endowed', passive_paths(prices, settings, endowed=True)))}
                    rows.append(dict(config_id=key, window_id=window['id'],
                                     cost_multiplier=cost, capacity_multiplier=float(capacity),
                                     metrics=metrics, controls=controls,
                                     paired_return_advantage={name: metrics['net_return'] - control['net_return']
                                                              for name, control in controls.items()}))
    return dict(manifest_id=manifest['id'], backend=backend,
                provenance_warning=manifest['provenance_warning'], results=rows)


def audit_paths(output, prices, config, backend='cuda',
                start='2026-11-15T00:00:00Z', *, path_ids=None):
    """Persist supplied frozen paths, Paper SQLite/CSV artifacts and FIFO reports.

    Comparison tolerances are absolute 1e-8, relative 1e-10 for arithmetic;
    transition states, reasons, order identities and fill counts match exactly.
    """
    prices = np.asarray(prices, dtype=float)
    if not np.array_equal(prices, freeze_prices(prices)):
        raise ValueError('audit requires supplied six-decimal frozen paths')
    reference = simulate(prices, config, backend='paper', trace=True)
    execution = simulate(prices, config, backend=backend, trace=True)
    ids = list(range(len(prices))) if path_ids is None else list(path_ids)
    if len(ids) != len(prices) or len(set(ids)) != len(ids):
        raise ValueError('one unique path id per supplied path required')
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    np.save(directory / 'prices.npy', prices, allow_pickle=False)
    _write(directory / 'inputs.json', dict(path_ids=ids, start=start, backend=backend,
        config=asdict(config), provenance_warning=PROVENANCE))
    audits = []
    exact = [COLUMNS.index(k) for k in ('fills', 'state', 'reason')]
    for i, path in enumerate(prices):
        target = directory / f'path-{i:05d}'
        _, artifacts = run_experiment(target, bars=close_bars(path, start), config=config.grid,
            rules=config.rules, capital=config.capital, fee_rate=config.fee_rate,
            slippage=config.slippage, interval='1h', market=Market.BINANCE_USDM,
            instrument_id='BINANCE:USDM:NVDAUSDT', scenario='CLOSE_ONLY_SCENARIO',
            synthetic_capacity=config.capacity, return_artifacts=True)
        persisted_columns = [k for k in COLUMNS if k in artifacts['rows'][0]
                             and k not in ('state', 'reason')]
        persisted = [[row[k] for k in persisted_columns] for row in artifacts['rows']]
        np.testing.assert_allclose(persisted,
            reference.trace[i][:, [COLUMNS.index(k) for k in persisted_columns]],
            rtol=1e-10, atol=1e-8)
        steps = {row['date']: step for step, row in enumerate(artifacts['rows'])}
        orders = artifacts['result']['orders']
        persisted_fills = np.asarray([[steps[t['execution_date']],
            int(t['order_id'].rsplit('-', 1)[1]), int(t['side'] == 'SELL'),
            t['grid_level'], t['executed_shares'], t['execution_price'], t['fee'],
            t['slippage'], int(orders[t['order_id']]['purpose'] == 'RISK_EXIT')]
            for t in artifacts['trades']], dtype=float).reshape(-1, 9)
        np.testing.assert_allclose(persisted_fills, reference.fills[i], rtol=1e-10, atol=1e-8)
        fifo = attribution(target)
        _write(target / 'fifo.json', fifo)
        np.savez(target / 'comparison.npz', paper_trace=reference.trace[i],
                 execution_trace=execution.trace[i], paper_fills=reference.fills[i],
                 execution_fills=execution.fills[i])
        np.testing.assert_allclose(execution.trace[i], reference.trace[i], rtol=1e-10, atol=1e-8)
        np.testing.assert_array_equal(execution.trace[i][:, exact], reference.trace[i][:, exact])
        np.testing.assert_allclose(execution.fills[i], reference.fills[i], rtol=1e-10, atol=1e-8)
        np.testing.assert_array_equal(execution.fills[i][:, [0, 1, 2, 3, 8]],
                                      reference.fills[i][:, [0, 1, 2, 3, 8]])
        for key in ('reconciliation_residual', 'reported_equity_difference'):
            if abs(fifo['summary'][key]) > Decimal('0.00000001'):
                raise ValueError(f'FIFO {key} for path {ids[i]}')
        audits.append(dict(path_id=ids[i], artifact_directory=str(target),
                           matched=True, fifo=fifo['summary']))
    report = dict(backend=backend, path_ids=ids, results=audits)
    _write(directory / 'audit.json', report)
    return report
