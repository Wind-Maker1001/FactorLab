"""Explicit source routing without source priority, fusion or certification."""

from typing import Mapping, Protocol

import pandas as pd

from factorlab.data.evidence import timestamp
from factorlab.errors import DataQualityError
from factorlab.universe.csi500 import CANONICAL_SYMBOL, strict_import_date
from .contracts import QueryResult, UnsupportedQueryError


class Adapter(Protocol):
    """An adapter exposes only the capabilities its underlying source has."""

    capabilities: frozenset[str]

    def read(self, kind: str, *, symbol: str | None, security_id: str | None,
             start: str, end: str, knowledge_as_of: str | None) -> QueryResult: ...


class DataHub:
    """Coordinate existing readers using explicit source names.

    ``knowledge_as_of=None`` requests retrospective engineering data, not PIT
    data. An explicit cutoff requires supported historical knowledge evidence;
    adapters must reject it rather than substitute effective or retrieval dates.
    """

    def __init__(self, sources: Mapping[str, Adapter]):
        self._sources = dict(sources)

    def capabilities(self) -> dict[str, frozenset[str]]:
        return {name: adapter.capabilities for name, adapter in self._sources.items()}

    def read(self, source: str, kind: str, *, start: str, end: str,
             symbol: str | None = None, security_id: str | None = None,
             knowledge_as_of: str | None = None) -> QueryResult:
        """Read an inclusive daily window; timestamps require explicit timezone.

        Symbols must include their exchange. Missing dates remain missing, and
        unsupported histories fail rather than falling back to current data.
        """
        if source not in self._sources:
            raise UnsupportedQueryError("unknown DataHub source: %s" % source)
        adapter = self._sources[source]
        if kind not in adapter.capabilities:
            raise UnsupportedQueryError("%s does not support %s" % (source, kind))
        lo, hi = strict_import_date(start, "start"), strict_import_date(end, "end")
        if lo > hi:
            raise DataQualityError("start must not exceed end")
        if symbol is not None and (not isinstance(symbol, str) or not CANONICAL_SYMBOL.fullmatch(symbol)):
            raise DataQualityError("explicit canonical symbol with exchange required")
        if kind == "corporate_actions":
            if not isinstance(security_id, str) or not security_id.strip() or symbol is not None:
                raise DataQualityError("explicit stable security_id required, not a guessed symbol mapping")
        elif security_id is not None:
            raise UnsupportedQueryError("security_id is only supported for corporate_actions")
        elif kind == "calendar" and symbol is not None:
            raise UnsupportedQueryError("calendar uses the source's bound benchmark; omit symbol")
        elif kind != "calendar" and symbol is None:
            raise DataQualityError("explicit symbol required")
        if knowledge_as_of is not None:
            timestamp(knowledge_as_of)
        result = adapter.read(kind, symbol=symbol, security_id=security_id, start=lo.date().isoformat(),
                              end=hi.date().isoformat(), knowledge_as_of=knowledge_as_of)
        if isinstance(result.data, pd.DataFrame):
            frame = result.data
            if not frame.columns.is_unique:
                raise DataQualityError("duplicate result columns")
            keys = [key for key in ("date", "symbol") if key in frame.columns]
            if keys and frame.duplicated(keys).any():
                raise DataQualityError("duplicate result keys; no silent source priority")
        elif isinstance(result.data, pd.DatetimeIndex) and result.data.has_duplicates:
            raise DataQualityError("duplicate calendar sessions")
        return result
