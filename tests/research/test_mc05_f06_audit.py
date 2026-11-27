from decimal import Decimal as D
import json

import numpy as np
import pytest

from factorlab.experiments.mc05_f06_audit import _selection, audit_selected
from factorlab.research.grid_simulation import BatchGridConfig, COLUMNS, simulate
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


@pytest.mark.parametrize('unsupported', [False, True])
def test_cpu_paper_fifo_selected_audit(tmp_path, monkeypatch, unsupported):
    config = BatchGridConfig(GridConfig(lower=80, upper=120, levels=4,
        initial_cash=1000, initial_inventory=0, initial_price=100, quantity=10,
        max_inventory=100, max_cash_committed=1000, breakout='pause', order_ttl=100,
        fee_bps=4, slippage_bps=6),
        GridRules(D('.1'), D('.1'), D('.01'), D('.1'), D('5')), capacity=D('1'))
    prices = np.array([[100, 89, 101, 89, 111, 89, 101],
                       [100, 100, 100, 100, 100, 100, 100]], dtype=float)
    terminal = simulate(prices, config, backend='paper').terminal
    assert terminal[1, COLUMNS.index('equity')] == 1000
    assert terminal[0, COLUMNS.index('fills')] > 0
    logs = np.diff(np.log(prices), axis=1)
    auxiliary = dict(efficiency_ratio=np.divide(np.abs(logs.sum(axis=1)),
        np.abs(logs).sum(axis=1), out=np.zeros(2), where=np.abs(logs).sum(axis=1) != 0),
        price_return=prices[:, -1] / prices[:, 0] - 1)
    calls = []

    def regenerate(path_id):
        calls.append(path_id)
        return prices[path_id]

    if unsupported:
        from factorlab.experiments import mc05_f06_audit
        from factorlab.research.grid_simulation import UnsupportedGridSimulation
        def refusing(values, config, *, backend, **kwargs):
            if backend != 'paper':
                raise UnsupportedGridSimulation('monetary boundary')
            return simulate(values, config, backend=backend, **kwargs)
        monkeypatch.setattr(mc05_f06_audit, 'simulate', refusing)
    receipt = audit_selected(tmp_path, definition={'start': '2026-11-15T00:00:00Z'},
        config=config, model='gbm', seed=1729, steps=6, terminal=terminal,
        auxiliary=auxiliary, regenerate=regenerate, backend='cpu')
    assert calls == [0, 1]
    assert receipt['population_size'] == 2
    assert receipt['independent_random_path_ids'] in ([0, 1], [1, 0])
    stored = json.loads((tmp_path / 'audit.json').read_text())
    assert len(stored['cases']) == 2
    for case in stored['cases']:
        assert case['independent_random'] and case['purposive_selected']
        assert float(case['fifo']['final_equity']) == pytest.approx(
            terminal[case['path_id'], COLUMNS.index('equity')], abs=5e-10)
        assert float(case['fifo']['reconciliation_residual']) == pytest.approx(0, abs=5e-10)
        if unsupported:
            assert case['batch_comparison'] == 'UNSUPPORTED_PAPER_ONLY'
            assert case['maximum_absolute_step_error_by_column'] is None
        else:
            assert set(case['maximum_absolute_step_error_by_column']) == set(COLUMNS)
        np.testing.assert_array_equal(np.load(case['prices_file']), prices[case['path_id']])
        assert (tmp_path / f"path-{case['path_id']}" / 'paper' / 'trades.csv').exists()
    assert not (tmp_path / 'audit.json.tmp').exists()


def test_random_selection_is_independent_of_terminal_and_shapes():
    terminal = np.zeros((50, len(COLUMNS)))
    auxiliary = dict(efficiency_ratio=np.linspace(0, 1, 50),
                     price_return=np.linspace(-.2, .2, 50))
    arguments = dict(definition={'audit_random_paths': 4}, model='jump_down',
                     seed=1729, steps=168, terminal=terminal, auxiliary=auxiliary)
    labels, random_ids, entropy = _selection(**arguments)
    changed = dict(arguments, terminal=terminal + 12,
                   auxiliary={key: values[::-1] for key, values in auxiliary.items()})
    assert _selection(**changed)[1:] == (random_ids, entropy)
    assert len(random_ids) == len(set(random_ids)) == 4
    assert all('independent_random' in labels[path_id] for path_id in random_ids)
    assert _selection(**dict(arguments, model='gbm'))[2] != entropy
    assert _selection(**dict(arguments, steps=720))[2] != entropy
