"""Append-only, explicitly selected engineering vintages; never admission."""

from dataclasses import asdict, replace
from io import BytesIO
import json
from pathlib import Path

import pandas as pd

from factorlab.data.evidence import (
    SourceArtifact, archive_bytes, json_bytes, require_hash, require_text, sha256, timestamp,
)
from factorlab.data.hub.contracts import QueryResult, UnsupportedQueryError
from factorlab.errors import DataQualityError


class GovernanceStore:
    """Persist source-bound table vintages without guessing historical availability.

    Importers supply original artifacts bound by the existing reader. Merely
    archiving a file does not establish historical availability. Versions are
    selected explicitly, never by vendor priority or acquisition time.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    def _slot(self, source, version, kind, symbol):
        for label, value in (("source", source), ("version", version),
                             ("kind", kind), ("symbol", symbol)):
            require_text(value, label)
        return self.root / "versions" / (sha256(json_bytes([source, version, kind, symbol])) + ".json")

    def capture(self, hub, source, kind, *, version, symbol, start, end,
                artifacts: tuple[SourceArtifact, ...], acquired_at,
                predecessor=None):
        """Archive an existing reader's retrospective result and original bytes.

        Historical availability remains UNKNOWN: neither reader manifests nor
        possession of supplier bytes prove when a record became knowable.
        """
        timestamp(acquired_at)
        result = hub.read(source, kind, symbol=symbol, start=start, end=end)
        if not isinstance(result.data, pd.DataFrame) or "date" not in result.data:
            raise UnsupportedQueryError("governance capture requires a dated table")
        if not artifacts:
            raise DataQualityError("original source artifacts required")
        # Bind artifacts to identities actually verified by the source adapter.
        provenance = result.metadata.provenance
        references = provenance.get("references", ())
        if references:
            required = {ref["source_sha256"] for ref in references}
        elif "source_file_sha256" in provenance:
            required = {provenance["manifest_sha256"], provenance["source_file_sha256"]}
        else:
            raise UnsupportedQueryError("capture supports pinned CSMAR and local bundles only")
        if {artifact.sha256 for artifact in artifacts} != required:
            raise DataQualityError("original artifacts must exactly match reader-bound source files")
        slot = self._slot(source, version, kind, symbol)
        if slot.exists():
            raise DataQualityError("version already exists; snapshots cannot be overwritten")
        if predecessor is not None:
            self._load(source, predecessor, kind, symbol)
        archived = []
        for artifact in artifacts:
            saved = archive_bytes(artifact.read(), self.root / "artifacts", artifact.source_id,
                                  artifact.locator, artifact.acquired_at)
            archived.append({"sha256": saved.sha256, "source_id": saved.source_id,
                             "locator": saved.locator, "acquired_at": saved.acquired_at})
        stream = BytesIO()
        result.data.to_parquet(stream, index=False)
        payload = archive_bytes(stream.getvalue(), self.root / "tables", source, version, acquired_at)
        document = {"source": source, "version": version, "kind": kind, "symbol": symbol,
                    "start": start, "end": end, "predecessor": predecessor,
                    "table_sha256": payload.sha256, "artifacts": archived,
                    "metadata": asdict(result.metadata), "acquired_at": acquired_at,
                    "availability": "UNKNOWN"}
        slot.parent.mkdir(parents=True, exist_ok=True)
        with slot.open("xb") as target:
            target.write(json_bytes(document))
        return self.read(source, kind, version=version, symbol=symbol, start=start, end=end)

    def _load(self, source, version, kind, symbol):
        document = json.loads(self._slot(source, version, kind, symbol).read_bytes())
        if [document[k] for k in ("source", "version", "kind", "symbol")] != [source, version, kind, symbol]:
            raise DataQualityError("governance identity mismatch")
        digest = require_hash(document["table_sha256"])
        content = (self.root / "tables" / (digest + ".bin")).read_bytes()
        if sha256(content) != digest:
            raise DataQualityError("governance table hash mismatch")
        for artifact in document["artifacts"]:
            raw = (self.root / "artifacts" / (require_hash(artifact["sha256"]) + ".bin")).read_bytes()
            if sha256(raw) != artifact["sha256"]:
                raise DataQualityError("governance source hash mismatch")
        return document, pd.read_parquet(BytesIO(content))

    def read(self, source, kind, *, version, symbol, start, end, knowledge_as_of=None):
        """Read an explicit immutable vintage; unknown historical times refuse PIT."""
        if knowledge_as_of is not None:
            timestamp(knowledge_as_of)
            raise UnsupportedQueryError("historical availability UNKNOWN; vintage is not PIT evidence")
        from factorlab.data.hub.contracts import SourceMetadata
        from factorlab.universe.csi500 import strict_import_date
        lo, hi = strict_import_date(start, "start"), strict_import_date(end, "end")
        if lo > hi:
            raise DataQualityError("start must not exceed end")
        document, frame = self._load(source, version, kind, symbol)
        if start < document["start"] or end > document["end"]:
            raise UnsupportedQueryError("window outside archived capture scope")
        frame = frame.loc[frame.date.between(lo, hi)].reset_index(drop=True)
        metadata = SourceMetadata(**dict(document["metadata"], evidence_status=tuple(
            document["metadata"]["evidence_status"])))
        return QueryResult(frame, replace(metadata, acquired_at=document["acquired_at"],
            time_fields={**metadata.time_fields, "effective": "date", "announcement": "UNKNOWN",
                         "provider_available": "UNKNOWN", "acquired": "metadata.acquired_at"},
            provenance={**metadata.provenance, "governance_version": version,
                        "predecessor": document["predecessor"],
                        "table_sha256": document["table_sha256"],
                        "original_artifacts": document["artifacts"],
                        "historical_availability": "UNKNOWN"}))

    def diagnose(self, source, version, other_source, other_version, kind, *, symbol, start, end):
        """Report exact field differences; never return a fused table."""
        left = self.read(source, kind, version=version, symbol=symbol, start=start, end=end)
        right = self.read(other_source, kind, version=other_version, symbol=symbol, start=start, end=end)
        keys = ["date", "symbol"] if "symbol" in left.data and "symbol" in right.data else ["date"]
        a, b = left.data.set_index(keys), right.data.set_index(keys)
        conflicts = []
        for key in a.index.union(b.index):
            for field in a.columns.union(b.columns):
                x = a.at[key, field] if key in a.index and field in a else None
                y = b.at[key, field] if key in b.index and field in b else None
                missing_x, missing_y = bool(pd.isna(x)), bool(pd.isna(y))
                if (missing_x and missing_y) or (not missing_x and not missing_y and x == y):
                    continue
                conflicts.append({"key": key, "field": field,
                    "type": ("MISSING" if missing_x or missing_y else
                             "STATUS" if field in {"trade_status", "is_st", "is_member"}
                             else "VALUE"),
                    "left": {"source": source, "version": version, "value": x,
                             "evidence": left.metadata.provenance["original_artifacts"]},
                    "right": {"source": other_source, "version": other_version, "value": y,
                              "evidence": right.metadata.provenance["original_artifacts"]}})
        return tuple(conflicts)
