"""Qbot FactorLab research namespace.

The package is deliberately independent from Qbot's legacy GUI, brokerage,
Qlib, and Backtrader modules. Importing it must remain side-effect free.
"""

from .errors import FactorLabError

__all__ = ["FactorLabError"]
__version__ = "0.1.0"
