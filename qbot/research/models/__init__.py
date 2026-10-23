"""Leakage-safe research model implementations.

The tree packages are optional and imported lazily by their model's ``fit``
method, so FactorLab's baseline models remain usable without native LightGBM
or XGBoost installations. This module has no dependency on legacy trading or
GUI packages.
"""

from .base import ModelInputError, ModelNotFittedError, ResearchModel
from .factor_score import FactorScoreModel
from .lightgbm import LightGBMDependencyError, LightGBMResearchModel
from .ridge import RidgeResearchModel
from .xgboost import XGBoostDependencyError, XGBoostResearchModel

__all__ = [
    "FactorScoreModel",
    "LightGBMDependencyError",
    "LightGBMResearchModel",
    "ModelInputError",
    "ModelNotFittedError",
    "ResearchModel",
    "RidgeResearchModel",
    "XGBoostDependencyError",
    "XGBoostResearchModel",
]
