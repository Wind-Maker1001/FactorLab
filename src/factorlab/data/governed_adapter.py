"""Optional DataHub bindings for explicitly selected immutable vintages."""

from pathlib import Path

from factorlab.data.corporate_actions.store import load_snapshot, publish_snapshot
from factorlab.data.hub.adapters import CorporateActionAdapter
from factorlab.data.hub.contracts import UnsupportedQueryError


class GovernedTableAdapter:
    """Bind one source/kind/version; DataHub's default adapters stay unchanged."""

    def __init__(self, store, *, source, kind, version):
        self.store, self.source, self.kind, self.version = store, source, kind, version
        self.capabilities = frozenset({kind})

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        if kind != self.kind or security_id is not None:
            raise UnsupportedQueryError("query differs from governed table binding")
        return self.store.read(self.source, kind, version=self.version, symbol=symbol,
                               start=start, end=end, knowledge_as_of=knowledge_as_of)


class GovernedCorporateActionAdapter(CorporateActionAdapter):
    """Archive original event evidence and reuse the existing historical selector.

    The content-addressed generation is the explicit version. Candidate events
    remain candidates, including proposed dividends and unverified terms.
    """

    def __init__(self, root: Path, *, generation: str):
        from factorlab.data.evidence import require_hash
        self.generation = require_hash(generation)
        super().__init__(load_snapshot(Path(root) / self.generation))

    def read(self, kind, **query):
        from dataclasses import replace
        result = super().read(kind, **query)
        return replace(result, metadata=replace(result.metadata, provenance={
            **result.metadata.provenance, "governance_generation": self.generation}))

    @classmethod
    def capture(cls, snapshot, root: Path):
        folder = publish_snapshot(snapshot, root)
        return cls(root, generation=folder.name)
