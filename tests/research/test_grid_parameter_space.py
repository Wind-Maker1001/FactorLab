from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from factorlab.experiments.grid_parameter_space import (
    describe, load_base, one_factor, sample_configs,
)


@pytest.fixture
def base():
    return load_base(Path(__file__).resolve().parents[2] / 'configs' / 'mc05_f02_grid_tail.json')


def test_frozen_definition_roundtrip(base, tmp_path):
    path = tmp_path / 'base.json'
    path.write_text(json.dumps(describe(base)), encoding='utf-8')
    restored = load_base(path)
    assert restored == base
    assert hash(restored) == hash(base)
    with pytest.raises(FrozenInstanceError):
        restored.grid.levels = 16
    assert describe(base)['rules']['quantity_unit'] == '0.01'


def test_all_one_factor_axes_and_infeasibility(base):
    rows = one_factor(base)
    assert rows[0][1:3] == ('baseline', None)
    assert rows[0][3] == base
    assert {value for _, axis, value, _ in rows if axis == 'levels'} == {4, 8, 16, 32}
    ranges = {value for _, axis, value, _ in rows if axis == 'range_pct'}
    assert {(5, 5), (40, 40), (10, 20), (20, 10)} <= ranges
    assert {value for _, axis, value, _ in rows if axis == 'quantity'} == {2, 5, 10, 20, 40}
    inventory = [row for row in rows if row[1] == 'initial_inventory']
    assert len(inventory) == 7
    assert any(config is None and value['reason'] for _, _, value, config in inventory)
    assert {value for _, _, value, config in inventory if config is not None} == {0, 1, 2, 4}
    configs_by_id = {}
    for identity, _, _, config in rows:
        if config is not None:
            configs_by_id.setdefault(config, set()).add(identity)
    assert all(len(identities) == 1 for identities in configs_by_id.values())
    for _, axis, value, config in rows:
        if config is None:
            continue
        assert config.capital == Decimal('1000')
        assert config.rules.quantity_unit == base.rules.quantity_unit
        assert Decimal(str(config.grid.fee_bps)) == config.fee_rate * 10000
        assert Decimal(str(config.grid.slippage_bps)) == config.slippage * 10000
        if axis == 'max_cash_committed_pct':
            assert config.grid.max_cash_committed == 1000 * value / 100
        if axis == 'range_pct':
            assert config.grid.lower == pytest.approx(base.grid.initial_price * (1 - value[0] / 100))
            assert config.grid.upper == pytest.approx(base.grid.initial_price * (1 + value[1] / 100))


def test_finite_sample_reproducible_distinct_and_audited(base):
    selected, rejected = sample_configs(base, 40, 1729)
    assert (selected, rejected) == sample_configs(base, 40, 1729)
    assert selected != sample_configs(base, 40, 2718)[0]
    assert len(selected) == len({identity for identity, _ in selected}) == 40
    assert len({config for _, config in selected}) == 40
    assert rejected
    assert all(row['reason'] and row['definition'] and row['id'] for row in rejected)
    assert any('initial inventory' in row['reason'] for row in rejected)
    json.dumps(rejected)
    for _, config in selected:
        _, _, params, integers = config.packed(4096)
        assert params[3] == float(config.fee_rate)
        assert params[4] == float(config.slippage)
        assert integers[3] == int(Decimal(config.grid.quantity) * config.rules.quantity_unit / config.rules.step_size)


def test_nonzero_inventory_uses_explicit_quantity_unit(base):
    # A low quote makes the legacy strategy's integer-unit validation admissible.
    grid = replace(base.grid, initial_price=1, lower=.8, upper=1.2)
    low_price = replace(base, grid=grid)
    row = next(row for row in one_factor(low_price) if row[1:3] == ('initial_inventory', 50))
    config = row[3]
    assert config.grid.initial_cash == 1000
    assert config.packed(4096)[3][4] == 50
    assert config.rules.quantity_unit == Decimal('.01')


def test_cost_mismatch_is_rejected(base, tmp_path):
    definition = describe(base)
    definition['fee_rate'] = '.0008'
    path = tmp_path / 'mismatch.json'
    path.write_text(json.dumps(definition), encoding='utf-8')
    with pytest.raises(ValueError, match='costs must agree'):
        load_base(path)


@pytest.mark.parametrize('n', [-1, 1.5, True])
def test_invalid_sample_size(base, n):
    with pytest.raises(ValueError, match='nonnegative integer'):
        sample_configs(base, n, 1)
