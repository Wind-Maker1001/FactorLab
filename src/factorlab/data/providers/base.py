"""Provider interfaces for FactorLab market data.

Providers return data in the canonical schema defined by
``factorlab.data.schema``.  Network- and vendor-specific details must not
leak into factor, label, or dataset code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd


DateLike = Union[str, pd.Timestamp]


@dataclass(frozen=True)
class MarketQualificationEvidence:
    """Pinned replay inputs, never a provider-supplied approval decision.

    The common admission boundary reparses the package and compares its
    financial semantics and exact tables with the requested consumption scope.
    Merely constructing this object grants no research or execution rights.
    """

    manifest: Path
    manifest_sha256: str
    source_version: str
    stocks: pd.DataFrame
    benchmark: pd.DataFrame


class MarketDataProvider(ABC):
    """Uniform interface implemented by all FactorLab market-data vendors."""

    def get_research_qualification(self) -> Optional[MarketQualificationEvidence]:
        """Return replayable evidence if available; unknown sources fail closed.

        Downloading and engineering reads do not require financial admission.
        Formal consumers must use the provider-independent admission function,
        rather than trusting this method, a provider name, or a PASS flag.
        """
        return None

    @abstractmethod
    def get_daily_bars(
        self, symbol: str, start: DateLike, end: DateLike
    ) -> pd.DataFrame:
        """Return canonical daily stock bars for the inclusive date range."""

    @abstractmethod
    def get_index_bars(
        self, symbol: str, start: DateLike, end: DateLike
    ) -> pd.DataFrame:
        """Return canonical daily index bars for the inclusive date range."""

    @abstractmethod
    def get_trade_calendar(
        self, start: DateLike, end: DateLike
    ) -> pd.DatetimeIndex:
        """Return benchmark trading sessions for the inclusive date range."""

    @abstractmethod
    def get_index_constituents(
        self, symbol: str, as_of: Optional[DateLike] = None
    ) -> pd.DataFrame:
        """Return a constituent snapshot when the provider supports it.

        A provider that only exposes the current snapshot must reject a
        historical ``as_of`` value.  It must never silently substitute current
        members for a point-in-time request.
        """
