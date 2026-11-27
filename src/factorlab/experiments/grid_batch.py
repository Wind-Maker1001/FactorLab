"""Shared frozen-quote preparation for registered static-grid studies."""
from decimal import Decimal
import json
from pathlib import Path

import numpy as np

from factorlab.research.grid_simulation import BatchGridConfig, freeze_prices
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules


def configuration(path):
    definition = json.loads(Path(path).read_text(encoding='utf-8'))
    config = BatchGridConfig(GridConfig(**definition['grid']),
        GridRules(**{k: Decimal(v) for k, v in definition['rules'].items()}),
        fee_rate=Decimal(definition['costs']['fee_rate']),
        slippage=Decimal(definition['costs']['slippage']), capacity=Decimal(definition['capacity']))
    return definition, config


def batches(definition, size, batch_size, seed):
    rng = np.random.default_rng(seed)
    data = definition['dataset']
    steps = definition['experiment']['steps']
    for begin in range(0, size, batch_size):
        count = min(batch_size, size - begin)
        shocks = rng.normal(data['drift'] - .5*data['volatility']**2,
                            data['volatility'], (count, steps))
        prices = data['initial_price'] * np.exp(np.column_stack((np.zeros(count), shocks.cumsum(axis=1))))
        yield begin, freeze_prices(prices)
