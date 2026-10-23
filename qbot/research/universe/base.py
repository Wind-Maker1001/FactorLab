"""Abstract interfaces for historical index membership."""

from abc import ABC, abstractmethod
from typing import List, Optional

import pandas as pd


class PointInTimeUniverseProvider(ABC):
    """Source of an index's constituents as known on a historical date."""

    @abstractmethod
    def get_constituents(
        self, index_symbol: str, date: pd.Timestamp
    ) -> pd.DataFrame:
        """Return the historical constituent snapshot for exactly ``date``."""


class PointInTimeUniverse(ABC):
    """Read-only, exact-date view of a point-in-time stock universe."""

    @abstractmethod
    def get_members(self, date: pd.Timestamp) -> List[str]:
        """Return canonical symbols valid on ``date`` or fail if unavailable."""

    def version(self) -> Optional[str]:
        """Return a stable content version when available."""

        return None
