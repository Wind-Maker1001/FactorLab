"""Finite, reproducible static-grid parameter enumeration for MC-GRID-OPT-01.

Legacy GridConfig validates endowed inventory in integer strategy units rather
than quantity_unit-scaled units. This limits supported endowed exposure; larger
requested inventories remain explicit rejections, not relaxed capital ceilings.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import random

from factorlab.errors import ConfigurationError
from factorlab.research.grid_simulation import BatchGridConfig
from factorlab.strategies.grid import GridConfig, GridStrategy
from factorlab.strategies.grid_execution import GridRules


def describe(config):
    """Return the complete economic definition, with exact decimals as strings."""
    return {
        'grid': asdict(config.grid),
        'rules': {key: str(value) for key, value in asdict(config.rules).items()},
        **{key: str(getattr(config, key))
           for key in ('capital', 'fee_rate', 'slippage', 'capacity')},
    }


def _build(definition):
    config = BatchGridConfig(
        GridConfig(**definition['grid']),
        GridRules(**{key: Decimal(value) for key, value in definition['rules'].items()}),
        **{key: Decimal(definition[key])
           for key in ('capital', 'fee_rate', 'slippage', 'capacity')},
    )
    if config.capital != Decimal('1000'):
        raise ValueError('MC-GRID-OPT-01 requires capital 1000')
    if (Decimal(str(config.grid.fee_bps)) != config.fee_rate * 10000
            or Decimal(str(config.grid.slippage_bps)) != config.slippage * 10000):
        raise ValueError('GridConfig and batch costs must agree')
    GridStrategy(config.grid)
    config.packed(4096)
    return config


def load_base(path):
    """Read a frozen batch definition or the existing grid-tail study JSON."""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    definition = {key: raw[key] for key in ('grid', 'rules', 'capacity')}
    definition['capital'] = raw.get('capital', '1000')
    for key in ('fee_rate', 'slippage'):
        definition[key] = raw[key] if key in raw else raw['costs'][key]
    return _build(definition)


# Range entries are lower/upper percentage distances from the frozen price.
_AXES = {
    'mode': ('arithmetic', 'geometric'),
    'levels': (4, 8, 16, 32),
    'range_pct': tuple((x, x) for x in (5, 10, 20, 30, 40))
                 + ((5, 10), (10, 5), (10, 20), (20, 10), (20, 40), (40, 20)),
    'max_cash_committed_pct': (10, 25, 50, 75, 100),
    'quantity': (2, 5, 10, 20, 40),
    'initial_inventory': (0, 1, 2, 4, 10, 50, 100),
    'max_inventory': (25, 50, 100, 200),
    'max_drawdown': (.05, .1, .2, .4),
    'max_turnover': (1, 5, 10, 20),
    'order_ttl': (24, 48, 100, 168),
    'breakout': ('pause', 'exit'),
    'fee_bps': (2, 4, 8, 16),
    'slippage_bps': (0, 6, 12, 24),
    'capacity': ('.02', '.05', '.1', '.2'),
    'min_notional': ('5', '10', '25'),
    # quantity_unit remains the frozen legacy-unit mapping, not a precision knob.
    'precision': (('0.01', '0.01'), ('0.001', '0.001'),
                  ('0.000001', '0.0001'), ('0.1', '0.01')),
}


def _apply(definition, axis, value):
    grid = definition['grid']
    if axis == 'range_pct':
        price = Decimal(str(grid['initial_price']))
        grid['lower'] = float(price * (1 - Decimal(value[0]) / 100))
        grid['upper'] = float(price * (1 + Decimal(value[1]) / 100))
    elif axis == 'max_cash_committed_pct':
        grid['max_cash_committed'] = float(Decimal(definition['capital']) * value / 100)
    elif axis in ('fee_bps', 'slippage_bps'):
        grid[axis] = value
        definition['fee_rate' if axis == 'fee_bps' else 'slippage'] = str(Decimal(value) / 10000)
    elif axis == 'capacity':
        definition[axis] = value
    elif axis == 'min_notional':
        definition['rules'][axis] = value
    elif axis == 'precision':
        definition['rules']['tick_size'], definition['rules']['step_size'] = value
    else:
        grid[axis] = value


def _identity(definition):
    canonical = deepcopy(definition)
    for key in ('capital', 'fee_rate', 'slippage', 'capacity'):
        canonical[key] = str(Decimal(canonical[key]).normalize())
    canonical['rules'] = {key: str(Decimal(value).normalize())
                          for key, value in canonical['rules'].items()}
    canonical['grid'] = {
        key: str(Decimal(str(value)).normalize()) if type(value) in (int, float) else value
        for key, value in canonical['grid'].items()
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
    return 'grid-' + sha256(encoded.encode()).hexdigest()[:20]


def _evaluate(definition):
    try:
        return _build(definition), None
    except (ValueError, ConfigurationError) as error:
        return None, {'id': _identity(definition), 'definition': definition,
                      'reason': str(error), 'error': type(error).__name__}


def _one_factor_definitions(base):
    definition = describe(base)
    yield 'baseline', None, definition
    for axis, values in _AXES.items():
        for value in values:
            candidate = deepcopy(definition)
            _apply(candidate, axis, value)
            yield axis, value, candidate


def one_factor(base):
    """Include baseline and every axis value; infeasible entries have config=None.

    IDs identify definitions, so duplicate baseline-equivalent points share IDs.
    Rejected values are dictionaries with ``requested`` and ``reason`` fields.
    """
    _build(describe(base))
    rows = []
    for axis, value, definition in _one_factor_definitions(base):
        config, rejection = _evaluate(definition)
        if rejection is not None:
            value = {'requested': value, 'reason': rejection['reason']}
        rows.append((_identity(definition), axis, value, config))
    return rows


def sample_configs(base, n, seed):
    """Choose n distinct feasible definitions from a finite stratified pool.

    Each axis is independently shuffled with balanced marginal coverage. The pool
    includes all one-factor points and max(512, 8*n) joint points, not a Cartesian
    product. Infeasibility is retained; an exhausted pool raises instead of retrying.
    """
    if type(n) is not int or n < 0:
        raise ValueError('n must be a nonnegative integer')
    _build(describe(base))
    rng = random.Random(seed)
    size = max(512, 8 * n)
    columns = {}
    for axis, values in _AXES.items():
        column = [values[i % len(values)] for i in range(size)]
        rng.shuffle(column)
        columns[axis] = column
    definitions = [definition for _, _, definition in _one_factor_definitions(base)]
    for index in range(size):
        candidate = describe(base)
        for axis, column in columns.items():
            _apply(candidate, axis, column[index])
        definitions.append(candidate)
    feasible, rejected, seen = [], [], set()
    for definition in definitions:
        identity = _identity(definition)
        if identity in seen:
            continue
        seen.add(identity)
        config, rejection = _evaluate(definition)
        if rejection is None:
            feasible.append((identity, config))
        else:
            rejected.append(rejection)
    if n > len(feasible):
        raise ValueError(f'finite pool has {len(feasible)} feasible distinct configs; requested {n}')
    return rng.sample(feasible, n), rejected
