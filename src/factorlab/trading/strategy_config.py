"""Explicit local strategy selection. Model pickle paths must be trusted."""
import json
from decimal import Decimal
from pathlib import Path

from .strategy import DualMAStrategy


def select_strategy(name, *, interval_seconds=3600, config_path=None):
    if name == 'threshold':
        if config_path is not None:
            raise ValueError('threshold does not accept a model deployment')
        return None
    if name == 'dual-ma':
        if config_path is not None:
            raise ValueError('DualMA does not accept a model deployment')
        return DualMAStrategy(interval_seconds=interval_seconds)
    if name != 'frozen-model' or config_path is None:
        raise ValueError('frozen-model requires a trusted deployment file')
    from .frozen_strategy import (FrozenFeature, FrozenDeploymentBinding,
                                  FixedScoreConversion, FrozenModelStrategy)
    path = Path(config_path)
    value = json.loads(path.read_text(encoding='utf-8'))
    binding = value['binding']
    binding['features'] = tuple(FrozenFeature(**f) for f in binding['features'])
    if binding['interval_seconds'] != interval_seconds:
        raise ValueError('deployment interval differs from requested bars')
    conversion = value['conversion']
    conversion['target_weight'] = Decimal(conversion['target_weight'])
    bundle = Path(value['bundle'])
    if not bundle.is_absolute():
        bundle = path.parent / bundle
    return FrozenModelStrategy(bundle, binding=FrozenDeploymentBinding(**binding),
        conversion=FixedScoreConversion(**conversion), **value['identity'])
