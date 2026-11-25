"""Restore the native grid engine from its economic deployment record."""


def restore_grid(deployment):
    from factorlab.backtest.fees import FeeRule, FeeSchedule
    from .grid import GridConfig
    from .grid_execution import GridExecution, GridRules

    values = dict(deployment)
    values['config'] = GridConfig(**values['config'])
    values['rules'] = GridRules(**values['rules'])
    values['fees'] = FeeSchedule([FeeRule(**rule) for rule in values['fees']])
    kind = values.pop('r10_strategy', None)
    if kind is None:
        return GridExecution(**values)
    from factorlab.experiments.grid_funding_adaptive import (
        FundingSensitiveGrid, FundingPolicy, VolatilityAdaptiveGrid, VolatilityPolicy,
    )
    if kind == 'B_FUNDING_SENSITIVE_GRID':
        values['funding_policy'] = FundingPolicy(**values['funding_policy'])
        values['funding_events'] = [tuple(event) for event in values['funding_events']]
        return FundingSensitiveGrid(**values)
    if kind == 'C_VOL_ADAPTIVE_GRID':
        values['vol_policy'] = VolatilityPolicy(**values['vol_policy'])
        return VolatilityAdaptiveGrid(**values)
    raise ValueError('unknown persisted Grid strategy')
