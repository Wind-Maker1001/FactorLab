"""Explicit, evidence-preserving access to existing FactorLab data sources."""

_EXPORTS = {
    "CorporateActionAdapter": ".adapters",
    "CsmarAdapter": ".adapters",
    "MembershipAdapter": ".adapters",
    "ProviderAdapter": ".adapters",
    "SecurityDataAdapter": ".adapters",
    "QueryResult": ".contracts",
    "SourceMetadata": ".contracts",
    "UnsupportedQueryError": ".contracts",
    "DataHub": ".hub",
}

__all__ = [
    "DataHub", "QueryResult", "SourceMetadata", "UnsupportedQueryError",
    "ProviderAdapter", "CsmarAdapter", "MembershipAdapter", "SecurityDataAdapter",
    "CorporateActionAdapter",
]


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module_name, __name__), name)


def __dir__():
    return sorted(list(globals()) + __all__)
