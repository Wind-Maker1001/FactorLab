"""Selected-path Paper/FIFO evidence, not population risk estimation."""
from pathlib import Path

import numpy as np

from factorlab.experiments.grid_fifo import attribution
from factorlab.experiments.grid_monte_carlo import close_bars
from factorlab.experiments.grid_paper import run_experiment
from factorlab.experiments.mc_grid_tail_convergence import write
from factorlab.research.grid_simulation import COLUMNS, UnsupportedGridSimulation, simulate
from factorlab.trading.contracts import Market


RTOL = 1e-12
ATOL = 5e-10


def _selection(*, definition, model, seed, steps, terminal, auxiliary):
    size = len(terminal)
    count = definition.get('audit_random_paths', 2)
    if type(count) is not int or count < 0:
        raise ValueError('audit_random_paths must be a nonnegative integer')
    # Explicit namespace words separate audit draws from path generation.
    entropy = [0x46303641, 0x55444954, int(seed), int(steps), *str(model).encode('utf-8')]
    rng = np.random.default_rng(np.random.SeedSequence(entropy))
    random_ids = rng.choice(size, min(count, size), replace=False).tolist()
    labels = {}

    def add(path_id, label):
        labels.setdefault(int(path_id), []).append(label)

    for path_id in random_ids:
        add(path_id, 'independent_random')
    equity = terminal[:, COLUMNS.index('equity')]
    add(np.argmin(equity), 'minimum_terminal_equity')
    add(np.argmax(equity), 'maximum_terminal_equity')
    efficiency = np.asarray(auxiliary['efficiency_ratio'])
    returns = np.asarray(auxiliary['price_return'])
    # Median shape within each half avoids selecting another extreme path.
    split = np.median(efficiency)
    for label, mask in [('typical_oscillating', efficiency <= split),
                        ('typical_directional', efficiency >= split)]:
        ids = np.flatnonzero(mask)
        distance = np.abs(efficiency[ids] - np.median(efficiency[ids]))
        return_distance = np.abs(returns[ids] - np.median(returns[ids]))
        chosen = np.lexsort((ids, return_distance, distance))[0]
        add(ids[chosen], label)
    return labels, random_ids, entropy


def audit_selected(output, *, definition, config, model, seed, steps, terminal,
                   auxiliary, regenerate, backend='cuda'):
    """Replay unique selected global path IDs using their exact frozen quotes.

    ``auxiliary`` maps ``efficiency_ratio`` (absolute endpoint log return /
    sum of absolute log increments) and ``price_return`` to population arrays.
    ``regenerate(path_id)`` returns the frozen one-dimensional price array.
    Returns the JSON-compatible receipt also written to ``output/audit.json``.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    labels, random_ids, entropy = _selection(definition=definition, model=model,
        seed=seed, steps=steps, terminal=terminal, auxiliary=auxiliary)
    receipt = dict(schema_version=1, status='IN_PROGRESS', evidence='RESEARCH_SIMULATION', model=model,
        seed=int(seed), steps=int(steps), population_size=len(terminal), backend=backend,
        audit_policy='independent random plus purposive shape/extreme cases; never population risk estimates',
        random_domain='MC05_F06_AUDIT_V1', random_entropy=entropy,
        requested_random_paths=definition.get('audit_random_paths', 2),
        independent_random_path_ids=random_ids, selected_unique_paths=len(labels),
        selection_policy='min/max equity; median efficiency in lower/upper population halves; median price return tie-break; path ID final tie-break',
        efficiency_ratio='abs(log endpoint return) / sum(abs(log increments)); flat paths have zero efficiency',
        costs=dict(fee_rate=str(config.fee_rate), slippage=str(config.slippage),
                   slippage_accounting='included in execution price; not deducted twice'),
        funding='NOT_SIMULATED', market='BINANCE_USDM',
        instrument_id='BINANCE:USDM:NVDAUSDT', rtol=RTOL, atol=ATOL,
        columns=list(COLUMNS), cases=[])
    write(output / 'audit.json', receipt, atomic=True)
    for path_id, case_labels in sorted(labels.items()):
        directory = output / f'path-{path_id}'
        directory.mkdir(parents=True, exist_ok=True)
        prices = np.asarray(regenerate(path_id))
        np.save(directory / 'prices.npy', prices)
        batch = prices[np.newaxis, :]
        refusal = None
        try:
            actual = simulate(batch, config, backend=backend, trace=True, batch_size=1)
        except UnsupportedGridSimulation as error:
            refusal = str(error)
        paper = simulate(batch, config, backend='paper', trace=True)
        if refusal is None:
            np.testing.assert_allclose(actual.trace, paper.trace, rtol=RTOL, atol=ATOL,
                                       err_msg=f'path {path_id}: {COLUMNS}')
            np.testing.assert_allclose(actual.fills[0], paper.fills[0], rtol=RTOL, atol=ATOL)
        else:
            actual = paper
        np.testing.assert_allclose(actual.terminal[0], terminal[path_id], rtol=RTOL, atol=ATOL)
        paper_directory = directory / 'paper'
        run_experiment(paper_directory, bars=close_bars(prices, definition['start']),
            config=config.grid, rules=config.rules, capital=config.capital,
            fee_rate=config.fee_rate, slippage=config.slippage, interval='1h',
            market=Market.BINANCE_USDM, instrument_id=receipt['instrument_id'],
            scenario='CLOSE_ONLY_SCENARIO', synthetic_capacity=config.capacity,
            research_wal=True)
        fifo = attribution(paper_directory)['summary']
        equity = float(actual.terminal[0, COLUMNS.index('equity')])
        np.testing.assert_allclose(float(fifo['final_equity']), equity, rtol=RTOL, atol=ATOL)
        exposure = np.abs(actual.trace[0, :, COLUMNS.index('inventory')] * prices)
        fill_steps = actual.fills[0][:, 0].astype(int)
        lifetime = dict(mean_exposure_usdt=float(exposure.mean()),
            peak_exposure_usdt=float(exposure.max()),
            holding_observations=int(np.count_nonzero(actual.trace[0, :, COLUMNS.index('inventory')])),
            last_fill_step=int(fill_steps.max()) if len(fill_steps) else None,
            fills_after_initial_ttl=int(np.count_nonzero(fill_steps > config.grid.order_ttl)),
            tail_observations_after_last_fill=int(steps-fill_steps.max()) if len(fill_steps) else steps,
            interpretation='selected-path trace diagnostics, not full-population statistics')
        case = dict(path_id=path_id, labels=case_labels, lifetime=lifetime,
            batch_comparison='MATCHED' if refusal is None else 'UNSUPPORTED_PAPER_ONLY',
            unsupported_reason=refusal,
            independent_random='independent_random' in case_labels,
            purposive_selected=any(label != 'independent_random' for label in case_labels),
            population_size=len(terminal), observations=len(prices),
            efficiency_ratio=float(auxiliary['efficiency_ratio'][path_id]),
            price_return=float(auxiliary['price_return'][path_id]),
            terminal=dict(zip(COLUMNS, actual.terminal[0].tolist())),
            maximum_absolute_step_error_by_column=(dict(zip(COLUMNS,
                np.max(np.abs(actual.trace[0] - paper.trace[0]), axis=0).tolist()))
                if refusal is None else None),
            fills=actual.fills[0].tolist(), fifo=fifo,
            prices_file=str((directory / 'prices.npy').resolve()),
            paper_directory=str(paper_directory.resolve()))
        write(directory / 'audit.json', case, atomic=True)
        receipt['cases'].append(case)
        write(output / 'audit.json', receipt, atomic=True)
    receipt['status'] = ('PASSED_WITH_PAPER_ONLY' if any(
        case['unsupported_reason'] is not None for case in receipt['cases']) else 'PASSED')
    write(output / 'audit.json', receipt, atomic=True)
    return receipt
