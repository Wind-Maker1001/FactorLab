"""Explicit, evidence-preserving access to existing FactorLab data sources."""

from .adapters import (
    CorporateActionAdapter, CsmarAdapter, MembershipAdapter, ProviderAdapter,
    SecurityDataAdapter,
)
from .contracts import QueryResult, SourceMetadata, UnsupportedQueryError
from .hub import DataHub

__all__ = [
    "DataHub", "QueryResult", "SourceMetadata", "UnsupportedQueryError",
    "ProviderAdapter", "CsmarAdapter", "MembershipAdapter", "SecurityDataAdapter",
    "CorporateActionAdapter",
]
