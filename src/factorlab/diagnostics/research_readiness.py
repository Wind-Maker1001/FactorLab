"""Source-pinned engineering research diagnostics, separate from admission."""
from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from factorlab.data.evidence import json_bytes, sha256, table_sha256
from factorlab.data.hub.contracts import QueryResult
from factorlab.diagnostics.candidate_preflight import BOUNDARY
from factorlab.diagnostics.research_coverage import (
    KEYS, OHLC, factor_diagnostics, field_profile, join_members,
    label_availability, window_profiles,
)
from factorlab.universe.filters import UniverseFilterConfig, build_filter_flags


KINDS = ('bars', 'membership', 'calendar', 'benchmark', 'status', 'listings', 'corporate_actions')


def research_readiness(inputs: dict[str, QueryResult], *, identities: dict[str, tuple[str, str]],
                       start: str, end: str, research_start: str, research_end: str,
                       batch_symbols: int = 32):
    """Diagnose one market source at a time; dates are inclusive input boundaries.

    Identities explicitly select each evidence source/version, including ancillary
    sources. Missing evidence remains UNKNOWN, never inferred from price rows.
    ``end`` bounds label observations; factors consume no rows after research_end.
    Source time fields are references, not certification of historical knowledge.
    """
    lo, hi, first, last = map(pd.Timestamp, (start, end, research_start, research_end))
    if not lo <= first <= last <= hi:
        raise ValueError('research window must lie inside input window')
    if set(inputs) - set(KINDS) or set(identities) != set(inputs):
        raise ValueError('explicit identity required for each supported research input')
    frames, bindings = {}, {}
    for kind, result in inputs.items():
        metadata = result.metadata
        if (metadata.source_id, metadata.data_version) != identities[kind] or not metadata.data_version:
            raise ValueError('research source/version differs from selected identity: ' + kind)
        if kind == 'corporate_actions':
            # CorporateActionAdapter already applies effective/knowledge selection
            # to immutable event vintages; do not flatten away their identities.
            bindings[kind] = dict(source=metadata.source_id, version=metadata.data_version,
                effective_date=dict(start=start, end=end), known_at='UNKNOWN',
                source_kind=metadata.provenance.get('source_kind', metadata.provenance.get('history_kind', 'UNKNOWN')),
                content_identity=sha256(json_bytes(result.data)), metadata=asdict(metadata),
                event_vintages=len(result.data), historical_qualification='UNKNOWN',
                event_references=[dict(security_id=event.security_id, event_id=event.event_id,
                    revision=event.revision, source=event.source_id,
                    content_identity=event.source_sha256, effective_date=event.effective_date,
                    announced_at=event.announced_at, available_at_claim=event.available_at,
                    known_at='UNKNOWN') for event in result.data])
            continue
        frame = result.data.copy()
        if not frame.columns.is_unique:
            raise ValueError('duplicate research columns: ' + kind)
        if 'date' in frame:
            frame['date'] = pd.to_datetime(frame.date)
            if frame.date.isna().any() or frame.date.dt.tz is not None or not frame.date.eq(frame.date.dt.normalize()).all():
                raise ValueError('research dates must be timezone-naive daily keys')
            frame = frame.loc[frame.date.between(lo, hi)].copy()
        keys = [key for key in KEYS if key in frame]
        if kind != 'corporate_actions' and keys and frame.duplicated(keys).any():
            raise ValueError('duplicate research keys: ' + kind)
        content = table_sha256(frame)
        # A supplied table digest is an identity assertion, not optional advice.
        expected_content = metadata.provenance.get('table_sha256')
        if expected_content is not None and expected_content != table_sha256(result.data):
            raise ValueError('research content identity mismatch: ' + kind)
        frames[kind] = frame
        bindings[kind] = dict(source=metadata.source_id, version=metadata.data_version,
            effective_date=dict(start=start, end=end),
            known_at='UNKNOWN',
            historical_version=metadata.provenance.get('historical_version', 'UNKNOWN'),
            source_kind=metadata.provenance.get('source_kind', 'UNKNOWN'),
            content_identity=content, metadata=asdict(metadata), historical_qualification='UNKNOWN')
    report = dict(schema='P3D07_RESEARCH_V1', boundary=BOUNDARY,
        observations_origin='COMPUTED_SHARED_D06', bindings=bindings,
        engineering_numerical='UNKNOWN', source_traceability='PASS' if inputs else 'UNKNOWN',
        historical_identity='UNKNOWN', formal_admission='FAIL', financially_admitted_days=0,
        qualified_window=None, missing_evidence=[kind for kind in KINDS if kind not in inputs],
        original14='UNKNOWN', candidates64='UNKNOWN', common78='UNKNOWN', windows=[],
        label_values_computed=False)
    if not {'bars', 'membership', 'calendar'}.issubset(frames):
        return report
    bars, members = frames['bars'], frames['membership']
    members = members.loc[members.date.between(first, last), KEYS].sort_values(KEYS).reset_index(drop=True)
    calendar = pd.DatetimeIndex(frames['calendar'].date.sort_values())
    if not pd.Index(members.date.unique()).isin(calendar).all():
        raise ValueError('member dates absent from selected calendar')
    report['membership'] = dict(member_days=len(members), securities=int(members.symbol.nunique()),
        observed_identity='BOUND_SOURCE_OBSERVATIONS', certified_identity='UNKNOWN', known_at='UNKNOWN')
    if members.empty:
        return report
    factor_bars = bars.loc[bars.date.le(last) & bars.symbol.isin(members.symbol)].copy()
    required = {'date', 'symbol', *('raw_' + f for f in OHLC), *('adj_' + f for f in OHLC),
                'volume', 'amount', 'turnover'}
    report['missing_factor_fields'] = sorted(required - set(bars))
    raw_fields = [name for name in ['raw_' + f for f in OHLC] + ['volume', 'amount', 'turnover'] if name in bars]
    joined = join_members(members, factor_bars)
    report['raw_fields'] = field_profile(joined, raw_fields, bindings['bars']['source'])
    absent = joined.loc[joined['_merge'].eq('left_only'), KEYS].copy()
    gap_counts = {'OBSERVED_MEMBER_MISSING_ROW_ELIGIBILITY_UNKNOWN': len(absent)}
    if 'listings' in frames:
        listings = frames['listings']
        if listings.symbol.duplicated().any():
            raise ValueError('multiple listing identities per symbol')
        absent = absent.merge(listings, on='symbol', how='left', validate='many_to_one')
        before = pd.Series(False, index=absent.index)
        after = before.copy()
        if 'listing_date' in absent:
            before = absent.date.lt(pd.to_datetime(absent.listing_date))
        if 'delist_date' in absent:
            after = absent.date.gt(pd.to_datetime(absent.delist_date))
        gap_counts = dict(PRE_LISTING_OBSERVED_BOUNDARY=int(before.sum()),
            POST_DELISTING_OBSERVED_BOUNDARY=int(after.sum()),
            OBSERVED_MEMBER_MISSING_ROW_ELIGIBILITY_UNKNOWN=int((~before & ~after).sum()))
    report['missing_rows'] = dict(by_reason=gap_counts, supplier_omission='UNKNOWN',
        identity_boundary_qualification='UNKNOWN', repaired=False)
    if report['missing_factor_fields']:
        return report
    factor_calendar = calendar[calendar <= last]
    factors, finite, snapshot, quality = factor_diagnostics(
        factor_bars[list(KEYS) + [c for c in bars if c in required - set(KEYS)]],
        members, factor_calendar, batch_symbols)
    numerical_failure = quality['invalid_input_rows'] > 0 or any(
        row['unavailable_member_days'] > 0 for row in report['raw_fields'])
    report.update(engineering_numerical='FAIL' if numerical_failure else 'PASS',
        catalog_snapshot_hash=snapshot, factors=factors.to_dict('records'), source_quality=quality,
        original14=int(finite.all14.sum()), candidates64=int(finite.all64.sum()),
        common78=int(finite.all78.sum()))
    if 'benchmark' not in frames:
        return report
    benchmark = frames['benchmark']
    labels = label_availability(bars, members, calendar,
        benchmark.loc[np.isfinite(benchmark.open) & benchmark.open.gt(0), 'date'])
    report['label_endpoints'] = dict(available=int(labels.label_endpoints_available.sum()),
        missing=int((~labels.label_endpoints_available).sum()), entry_offset=1, exit_offset=6,
        historical_maturity='UNKNOWN')
    if not {'status', 'listings'}.issubset(frames):
        return report
    status = frames['status']
    report['status_observations'] = field_profile(join_members(members, status),
        ['trade_status', 'is_st'], bindings['status']['source'])
    flags = build_filter_flags(
        factor_bars[KEYS + ['raw_' + f for f in OHLC] + ['volume']].rename(columns={'raw_' + f: f for f in OHLC}),
        status.loc[status.date.le(last)], frames['listings'], factor_calendar, UniverseFilterConfig())
    member_flags = members.merge(flags, on=KEYS, how='left', validate='one_to_one')
    labels['structural_eligible'] = member_flags.eligible.eq(True).to_numpy()
    frame = finite.merge(labels, on=KEYS, validate='one_to_one').sort_values(KEYS).reset_index(drop=True)
    research_calendar = calendar[(calendar >= first) & (calendar <= last)]
    candidates = []
    report['window_candidates'] = []
    for name, months in [('minimum_12_3_3', 12), ('expanded_24_3_3', 24)]:
        required_tail = research_calendar[0] + pd.DateOffset(months=months + 6) - pd.offsets.BDay(1)
        enough = research_calendar[-1] >= required_tail
        report['window_candidates'].append(dict(candidate=name,
            status='PASS' if enough else 'UNKNOWN',
            scope='ENGINEERING_CALENDAR_SPAN_ONLY', required_tail=str(required_tail.date())))
        if enough:
            candidates.append((name, months))
    report['windows'] = window_profiles(frame, research_calendar, candidates=candidates)
    report['structural_eligibility'] = 'OBSERVED_SOURCE_PROXY_NOT_PIT'
    return report
