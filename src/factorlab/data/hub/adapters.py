"""Thin adapters for existing, independently usable data components."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pandas as pd

from factorlab.data.corporate_actions.contracts import SourceSnapshot
from factorlab.data.corporate_actions.validation import select_events, validate_snapshot
from factorlab.data.csmar.binding import replay
from factorlab.data.csmar.prepare import market_candidate
from factorlab.data.csmar.profiles import MAPPINGS, PROFILES
from factorlab.data.evidence import date, json_bytes, sha256, timestamp
from factorlab.data.providers.base import MarketDataProvider
from factorlab.data.providers.local import LocalParquetProvider
from factorlab.errors import DataQualityError
from factorlab.universe.csi500 import DataFramePointInTimeUniverse
from factorlab.universe.status import load_verified_security_data
from .contracts import QueryResult, SourceMetadata, UnsupportedQueryError


def _retrospective_only(knowledge_as_of):
    if knowledge_as_of is not None:
        raise UnsupportedQueryError("historical availability UNKNOWN; knowledge_as_of unsupported")


class ProviderAdapter:
    """Delegate market reads, keeping unknown generic-provider identity explicit.

    Local bundle identity comes from its verified manifest. A generic provider
    has no version contract in MarketDataProvider, so its version stays UNKNOWN.
    Financial admission remains the original consumer's responsibility.
    """

    capabilities = frozenset({"daily_bars", "index_bars", "calendar"})

    def __init__(self, provider: MarketDataProvider):
        self.provider = provider

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        _retrospective_only(knowledge_as_of)
        if kind == "calendar":
            data = self.provider.get_trade_calendar(start, end)
        elif kind == "daily_bars":
            data = self.provider.get_daily_bars(symbol, start, end)
        else:
            data = self.provider.get_index_bars(symbol, start, end)
        metadata = SourceMetadata(source_id="UNKNOWN", data_version=None,
                                  time_fields={"effective": "date"},
                                  provenance={"provider_type": type(self.provider).__name__})
        if isinstance(self.provider, LocalParquetProvider):
            manifest = self.provider.get_bundle_manifest()
            metadata = replace(
                metadata, source_id=manifest["source"], data_version=self.provider.source_version,
                calendar_id=self.provider.cache_source_version(self.provider.benchmark_symbol, True),
                calendar_kind="BENCHMARK_SESSIONS",
                coverage="STATIC_BUNDLE; NO_FILL; HISTORICAL_AVAILABILITY_UNKNOWN",
                provenance={"manifest_sha256": self.provider.manifest_sha256,
                            "bundle_version": self.provider.bundle_version,
                            "declared_source_version": manifest["source_version"],
                            "source_file_sha256": manifest[
                                "stocks_file_sha256" if kind == "daily_bars"
                                else "benchmark_file_sha256"]},
            )
        return QueryResult(data, metadata)


class CsmarAdapter:
    """Read pinned Dalyr references through the existing native replay path.

    Only supported market-candidate fields and observed sessions are exposed.
    Announcements, historical membership and official calendars are not inferred
    from Dalyr. Replay scratch belongs outside the original source root.
    """

    capabilities = frozenset({"daily_bars", "security_status", "observed_sessions"})

    def __init__(self, references, *, source_root: Path, scratch: Path):
        self._references = deepcopy(tuple(references))
        if not self._references or any(ref["family"] != "TRDNEW_Dalyr" for ref in self._references):
            raise UnsupportedQueryError("CSMAR adapter requires pinned TRDNEW_Dalyr references")
        self.source_root, self.scratch = Path(source_root), Path(scratch)

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        _retrospective_only(knowledge_as_of)
        selected = [ref for ref in self._references if symbol[:6] in ref["scope"]["codes"]
                    and ref["scope"]["start"] <= end and ref["scope"]["end"] >= start]
        if not selected:
            raise UnsupportedQueryError("no pinned CSMAR scope for requested symbol/window")
        frames = []
        for ref in selected:
            projection = replay(ref, source_root=self.source_root, scratch=self.scratch)
            mask = projection.raw.symbol.eq(symbol)
            if not mask.any():
                continue
            scoped = replace(projection, **{
                name: getattr(projection, name).loc[mask].reset_index(drop=True)
                for name in ("raw", "native", "lineage")
            })
            frames.append(market_candidate(scoped, symbol))
        if not frames:
            raise UnsupportedQueryError("no target CSMAR records; coverage UNKNOWN")
        bars = pd.concat(frames, ignore_index=True)
        bars = bars.loc[bars.date.between(pd.Timestamp(start), pd.Timestamp(end))]
        if bars.duplicated(["date", "symbol"]).any():
            raise DataQualityError("overlapping CSMAR sources/duplicate keys; explicit source selection required")
        bars = bars.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
        identity = "csmar-replay:" + sha256(json_bytes(selected))
        metadata = SourceMetadata(
            source_id="CSMAR_TRDNEW_Dalyr_CANDIDATE", data_version=identity,
            calendar_id=identity + ":observed:" + symbol,
            calendar_kind="OBSERVED_SECURITY_SESSIONS_ONLY",
            coverage="PINNED_SCOPE_ONLY; OFFICIAL_CALENDAR_UNKNOWN; NO_FILL",
            field_semantics={
                ('raw_' + name if name in {'open', 'high', 'low', 'close'} else name): dict(
                    native_field='TRDNEW_Dalyr.' + native, vendor_documented_unit=unit,
                    normalized_unit=normalized, multiplier=multiplier,
                    evidence_level='REPLAY_VALIDATED_DICTIONARY_AND_CONVERSION',
                    historical_applicability='PINNED_NATIVE_SCOPE; NOT_PIT_CERTIFICATION',
                    dictionary_sha256=PROFILES['TRDNEW_Dalyr'][0])
                for native, (name, unit, normalized, multiplier) in MAPPINGS['TRDNEW_Dalyr'].items()
            },
            time_fields={"effective": "date", "announcement": "UNKNOWN",
                         "provider_available": "UNKNOWN", "acquired": "UNKNOWN"},
            provenance={"references": deepcopy(tuple(selected)),
                        "price_semantics": "CSMAR_Adjprcwd_CANDIDATE; NOT_CERTIFIED_HFQ",
                        "status_semantics": "RETROSPECTIVE_NATIVE_OBSERVATIONS_ONLY"},
        )
        if kind == "observed_sessions":
            data = pd.DatetimeIndex(bars.date)
        elif kind == "security_status":
            data = bars[["date", "symbol", "trade_status", "is_st"]].copy()
        else:
            data = bars
        semantics = dict(metadata.field_semantics)
        semantics['adj_close'] = dict(semantics['raw_close'], native_field='TRDNEW_Dalyr.Adjprcwd',
            price_semantics='COMPARABLE_REINVESTMENT_CLOSE; HFQ_UNVERIFIED')
        metadata = replace(metadata, field_semantics={name: value for name, value in semantics.items()
            if isinstance(data, pd.DataFrame) and name in data})
        return QueryResult(data, metadata)


class MembershipAdapter:
    """Expose validated exact-date membership, not a historical PIT certificate."""

    capabilities = frozenset({"membership"})

    def __init__(self, universe: DataFramePointInTimeUniverse, *, index_symbol: str):
        self.universe, self.index_symbol = universe, index_symbol

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        _retrospective_only(knowledge_as_of)
        if symbol != self.index_symbol:
            raise UnsupportedQueryError("membership index differs from bound index")
        # Endpoint checks reuse the exact-date source's missing-snapshot errors.
        self.universe.get_members(pd.Timestamp(start))
        self.universe.get_members(pd.Timestamp(end))
        frame = self.universe.daily
        frame = frame.loc[frame.date.between(pd.Timestamp(start), pd.Timestamp(end))].reset_index(drop=True)
        return QueryResult(frame, SourceMetadata(
            source_id="DataFramePointInTimeUniverse", data_version=self.universe.version(),
            coverage="EXACT_SUPPLIED_DATES_ONLY; HISTORICAL_AVAILABILITY_UNKNOWN",
            time_fields={"effective": "date"}, provenance={"index_symbol": self.index_symbol},
        ))


class SecurityDataAdapter:
    """Use the existing manifest-bound security-data loader, without a cache."""

    capabilities = frozenset({"security_status", "listings"})

    def __init__(self, root: Path, *, calendar, membership, universe_sha256: str):
        self.root = Path(root)
        self.calendar = pd.DatetimeIndex(calendar).copy()
        self.membership = membership.copy()
        self.universe_sha256 = universe_sha256

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        _retrospective_only(knowledge_as_of)
        status, listings, manifest = load_verified_security_data(
            self.root, calendar=self.calendar, membership=self.membership,
            universe_sha256=self.universe_sha256,
        )
        if symbol not in set(listings.symbol):
            raise UnsupportedQueryError("security absent from bound security-data bundle")
        if kind == "listings":
            frame = listings.loc[listings.symbol.eq(symbol)].reset_index(drop=True)
        else:
            frame = status.loc[status.symbol.eq(symbol) & status.date.between(
                pd.Timestamp(start), pd.Timestamp(end))].reset_index(drop=True)
        return QueryResult(frame, SourceMetadata(
            source_id=manifest["source"], data_version=manifest["security_data_version"],
            calendar_id=manifest["calendar_sha256"], calendar_kind="BOUND_BENCHMARK_SESSIONS",
            coverage="BOUND_SECURITY_BUNDLE; HISTORICAL_AVAILABILITY_UNKNOWN",
            time_fields={"effective": "listing_date" if kind == "listings" else "date"},
            provenance={"source_version": manifest["source_version"],
                        "status_sha256": manifest["status_sha256"],
                        "listings_sha256": manifest["listings_sha256"],
                        "universe_sha256": manifest["universe_sha256"]},
        ))


class CorporateActionAdapter:
    """Delegate event vintage selection; do not expose the unfiltered snapshot."""

    capabilities = frozenset({"corporate_actions"})

    def __init__(self, snapshot: SourceSnapshot):
        validate_snapshot(snapshot)
        self.snapshot = snapshot

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        if knowledge_as_of is None:
            # Retrospective selection still uses the source's actual acquisition,
            # never the wall clock or an invented infinity timestamp.
            cutoff = self.snapshot.acquired_at
        else:
            cutoff = knowledge_as_of
        view = select_events(self.snapshot, effective_through=end, knowledge_as_of=cutoff)
        # Preserve the existing known-revisions contract, including rescheduled
        # revisions whose new effective date is outside the requested window.
        relevant = {(e.security_id, e.event_id) for e in view.events
                    if e.security_id == security_id and date(start) <= date(e.effective_date) <= date(end)}
        events = tuple(e for e in view.events if (e.security_id, e.event_id) in relevant)
        coverage = tuple(c for c in self.snapshot.coverage if c.security_id == security_id
                         and timestamp(c.available_at) <= timestamp(cutoff)
                         and c.start <= end and c.end >= start)
        # Coverage stays explicit; an empty tuple cannot certify absence of events.
        return QueryResult(events, SourceMetadata(
            source_id=self.snapshot.source_id, data_version=self.snapshot.source_version,
            acquired_at=self.snapshot.acquired_at,
            coverage="SOURCE_COVERAGE_RECORDS_ONLY; EMPTY_IS_NOT_PROOF_OF_ABSENCE",
            time_fields={"effective": "effective_date", "announcement": "announced_at",
                         "provider_available": "available_at", "acquired": "metadata.acquired_at"},
            provenance={"coverage": coverage, "history_kind": self.snapshot.history_kind,
                        "synthetic_only": self.snapshot.synthetic_only,
                        "issues": self.snapshot.issues,
                        "knowledge_as_of": cutoff,
                        "artifact_sha256": tuple(a.sha256 for a in self.snapshot.artifacts)},
        ))
