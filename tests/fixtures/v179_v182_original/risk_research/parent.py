import numpy as np
from v50.strategy import V50Strategy


class BandParent(V50Strategy):
    """Preserve v50 order generation and its v49 account and quote checks."""
    def __init__(self, adjusted_close=None, adjusted_volume=None, adjusted_open=None, *, omit_sndk=False):
        super().__init__(adjusted_close, adjusted_volume, adjusted_open, rule="band", omit_sndk=omit_sndk)

    def eligible_symbols(self):
        return [s for s in self.close if s != "SNDK" or not self.omit_sndk]


def tilt_fixed_budget(weights, score, fraction):
    """Tilt active, scored sleeves; keep their combined nominal exposure unchanged."""
    active = weights.gt(0) & np.isfinite(score)
    if active.sum() < 2:
        return weights
    w = weights.loc[active]
    s = score.loc[active].clip(-2, 2)
    centered = s - np.average(s, weights=w)
    result = weights.copy()
    result.loc[active] = w * (1 + fraction * centered / max(1, centered.abs().max()))
    return result
