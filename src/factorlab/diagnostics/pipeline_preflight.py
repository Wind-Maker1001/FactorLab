"""Pinned ResearchPipeline input diagnostics, never an admission credential."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

from factorlab.application.research_pipeline import BOUNDARY, HubQuery
from factorlab.data.evidence import json_bytes, sha256, table_sha256
from factorlab.diagnostics.research_coverage import OHLC, label_availability
from factorlab.errors import FactorLabError
from factorlab.universe.csi500 import CANONICAL_SYMBOL

GROUPS = ('bars', 'benchmark', 'membership', 'status', 'listings', 'calendar')
FIELDS = {
    'bars': ('date', 'symbol', *('raw_' + f for f in OHLC),
             *('adj_' + f for f in OHLC), 'volume', 'amount', 'turnover'),
    'benchmark': ('date', 'symbol', 'open'),
    'membership': ('date', 'symbol'),
    'status': ('date', 'symbol', 'trade_status', 'is_st'),
    'listings': ('symbol', 'listing_date', 'delist_date'),
    'calendar': ('date',),
}
NUMERIC = {'bars': FIELDS['bars'][2:], 'benchmark': ('open',), 'status': ('trade_status', 'is_st')}


@dataclass(frozen=True)
class InputBinding:
    """Expected identity of one exact Hub response; units are declarations, not certification.

    Field origins refer to upstream native fields or archived members, not formulas
    reconstructing raw prices from adjusted prices. Each origin remains a claim.
    """
    query: HubQuery
    source_id: str
    content_sha256: str
    units: Mapping[str, str] = field(default_factory=dict)
    field_origins: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionBinding:
    """Supplementary event query using DataHub's stable security identity contract."""
    source: str
    version: str
    source_id: str
    security_id: str
    start: str
    end: str
    content_sha256: str


def content_identity(data):
    """Use existing table/event digest conventions, normalizing calendar envelopes only."""
    if isinstance(data, pd.DatetimeIndex):
        data = pd.DataFrame({'date': data})
    return table_sha256(data) if isinstance(data, pd.DataFrame) else sha256(json_bytes(data))


def _dates(values):
    dates = pd.DatetimeIndex(pd.to_datetime(values))
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ValueError('timezone-naive daily dates required')
    return dates


def _research_quality(observations, receipts, symbols):
    """Retrospective observations, never an eligibility mask or price repair."""
    tables = {group: pd.concat(parts, ignore_index=True) for group, parts in observations.items() if parts}
    # Overlapping query rows cannot establish a unique observation.
    indexed = {}
    for group, frame in tables.items():
        keys = ['symbol'] if group == 'listings' else [k for k in ('date', 'symbol') if k in frame]
        indexed[group] = frame.loc[~frame.duplicated(keys, keep=False)].set_index(keys)
    cells = []
    calendar = indexed.get('calendar')
    if calendar is not None:
        for symbol in symbols:
            listing = indexed.get('listings')
            lifetime = listing.loc[symbol] if listing is not None and symbol in listing.index else None
            bars, status, members = (indexed.get(g) for g in ('bars', 'status', 'membership'))
            for day in calendar.index.sort_values():
                key = (day, symbol)
                flags = []
                boundary = 'UNKNOWN'
                if lifetime is not None:
                    boundary = ('PRE_LISTING' if day < lifetime.listing_date else
                        'POST_DELISTING' if pd.notna(lifetime.delist_date) and day > lifetime.delist_date else 'WITHIN_OBSERVED_LIFETIME')
                has_bar = bars is not None and key in bars.index
                has_status = status is not None and key in status.index
                suspended = (has_status and pd.notna(status.loc[key, 'trade_status'])
                             and status.loc[key, 'trade_status'] == 0)
                if suspended:
                    flags.append('SUSPENDED_OBSERVATION')
                if not has_bar:
                    flags.append(boundary if boundary in {'PRE_LISTING', 'POST_DELISTING'} else
                        'EXPECTED_BAR_NOT_OBSERVED' if boundary == 'WITHIN_OBSERVED_LIFETIME' else 'BAR_COVERAGE_UNKNOWN')
                else:
                    row = bars.loc[key]
                    values = pd.to_numeric(row[list(NUMERIC['bars'])], errors='coerce')
                    if row[list(NUMERIC['bars'])].isna().any():
                        flags.append('RAW_FIELD_MISSING')
                    if not np.isfinite(values.to_numpy(dtype=float)).all():
                        flags.append('NONFINITE_OR_NONNUMERIC_FIELD')
                    for prefix in ('raw_', 'adj_'):
                        o, h, l, c = (values[prefix + f] for f in OHLC)
                        if all(pd.notna(v) for v in (o, h, l, c)) and (
                                l <= 0 or h < l or h < o or h < c or l > o or l > c):
                            flags.append(prefix.upper() + 'OHLC_RELATIONSHIP_INVALID')
                    if values[['volume', 'amount', 'turnover']].lt(0).any():
                        flags.append('NEGATIVE_MARKET_QUANTITY')
                    if boundary in {'PRE_LISTING', 'POST_DELISTING'}:
                        flags.append('LIFETIME_CONFLICT')
                numeric_issues = {'RAW_FIELD_MISSING', 'NONFINITE_OR_NONNUMERIC_FIELD',
                    'RAW_OHLC_RELATIONSHIP_INVALID', 'ADJ_OHLC_RELATIONSHIP_INVALID', 'NEGATIVE_MARKET_QUANTITY'}
                calculable = has_bar and not numeric_issues.intersection(flags)
                member = members is not None and key in members.index
                if member and 'is_member' in members:
                    value = members.loc[key, 'is_member']
                    member = pd.notna(value) and value == True
                if not member:
                    flags.append('HISTORICAL_MEMBERSHIP_UNKNOWN')
                normal_status = (has_status and pd.notna(status.loc[key, 'trade_status'])
                                 and status.loc[key, 'trade_status'] == 1)
                if calculable and normal_status and boundary == 'WITHIN_OBSERVED_LIFETIME':
                    flags.insert(0, 'NORMAL_TRADING_OBSERVATION')
                if not normal_status and not suspended:
                    flags.append('STATUS_UNKNOWN')
                cells.append(dict(symbol=symbol, date=str(day.date()),
                    numeric_calculable=bool(calculable), observation_flags=flags,
                    observed_listing_boundary=boundary,
                    membership_observation='POSITIVE_OBSERVATION' if member else 'UNKNOWN',
                    historical_identity_and_timing='UNKNOWN', formal_admission='BLOCKED'))
    contracts = []
    for receipt in receipts:
        metadata = receipt.get('metadata', {})
        semantics = metadata.get('field_semantics', {})
        for name in NUMERIC.get(receipt['group'], ()):
            declaration = receipt['units'].get(name, 'UNKNOWN')
            evidence = semantics.get(name, {})
            normalized = evidence.get('normalized_unit', 'UNKNOWN')
            contracts.append(dict(group=receipt['group'], query=receipt['query'], field=name,
                declared_unit=declaration, semantics=evidence,
                source_and_unit_explained=(receipt.get('identity_and_structure_checked', False)
                    and evidence.get('evidence_level') == 'REPLAY_VALIDATED_DICTIONARY_AND_CONVERSION'
                    and 'UNKNOWN' not in normalized.upper()),
                historical_unit_contract='UNKNOWN' if 'UNKNOWN' in normalized.upper() else 'SOURCE_SCOPED',
                historical_pit='UNKNOWN'))
    return dict(scope='RETROSPECTIVE_QUERY_OBSERVATIONS; NOT_A_SELECTION_MASK',
        fields=contracts, observations=cells,
        numeric_calculable_rows=sum(row['numeric_calculable'] for row in cells),
        historical_identity_and_timing='UNKNOWN', formal_admission='BLOCKED')


def pipeline_preflight(hub, bindings: Mapping[str, tuple[InputBinding, ...]], *,
                       symbols: tuple[str, ...], start: str, end: str,
                       research_start: str, research_end: str, warmup_sessions: int,
                       holding_days: int, execution_tail_sessions: int,
                       actions: tuple[ActionBinding, ...] = (), require_actions: bool = True,
                       requested_formal: bool = False):
    """Read once per pin and diagnose explicit inclusive windows without source fusion.

    Sparse membership is a list of observed positive member-days; absent membership
    is UNKNOWN, never a reconstructed negative history. Listing boundaries classify
    nonexistent observations only at the observed-source level, not certified PIT.
    All knowledge-time claims remain in metadata; acquired_at is never known_at.
    """
    lo, hi, first, last = _dates([start, end, research_start, research_end])
    if not lo <= first <= last <= hi:
        raise ValueError('research interval must lie within the explicit input interval')
    if (not symbols or len(set(symbols)) != len(symbols)
            or any(not CANONICAL_SYMBOL.fullmatch(s) for s in symbols)):
        raise ValueError('unique canonical securities required')
    if warmup_sessions < 0 or holding_days < 1 or execution_tail_sessions < 1:
        raise ValueError('nonnegative warmup and positive holding/execution horizons required')
    issues, receipts, frames, action_receipts = [], [], {}, []
    observations = {group: [] for group in GROUPS}

    def fail(code, group, **detail):
        issues.append(dict(code=code, group=group, **detail))

    for extra in sorted(set(bindings) - set(GROUPS)):
        fail('UNSUPPORTED_GROUP', extra)
    completeness = {g: bool(bindings.get(g)) for g in GROUPS}
    for group in GROUPS:
        pins = bindings.get(group, ())
        if not pins:
            fail('MISSING_QUERY', group)
            continue
        if group in {'calendar', 'benchmark'} and len(pins) != 1:
            fail('ONE_AUTHORITATIVE_SOURCE_REQUIRED', group)
            continue
        parts = []
        for pin in pins:
            q = pin.query
            receipt = dict(group=group, query=asdict(q), expected_source_id=pin.source_id,
                expected_content_sha256=pin.content_sha256, units=dict(pin.units),
                field_origins=dict(pin.field_origins), status='FAIL', known_at='UNKNOWN')
            receipts.append(receipt)
            try:
                if not all((q.source, q.version, q.kind, pin.source_id, pin.content_sha256)):
                    raise ValueError('explicit source, version, kind and content identity required')
                qlo, qhi = _dates([q.start, q.end])
                if qlo > qhi or qlo < lo or qhi > hi:
                    raise ValueError('query exceeds declared input bounds')
                if group != 'calendar' and not q.symbol:
                    raise ValueError('explicit query security required')
                if group in {'bars', 'membership', 'status', 'listings'} and q.symbol not in symbols:
                    raise ValueError('query security outside selection')
                result = hub.read(q.source, q.kind, start=q.start, end=q.end, symbol=q.symbol)
                receipt['metadata'] = asdict(result.metadata)
                receipt['observed_content_sha256'] = content_identity(result.data)
                if result.metadata.source_id != pin.source_id or result.metadata.data_version != q.version:
                    raise ValueError('source/version mismatch with ResearchPipeline query')
                if receipt['observed_content_sha256'] != pin.content_sha256:
                    raise ValueError('content identity mismatch')
                declared = result.metadata.provenance.get('research_table_sha256')
                if declared is not None and declared != receipt['observed_content_sha256']:
                    raise ValueError('provider scoped content identity mismatch')
                if group == 'calendar' and not isinstance(result.data, pd.DatetimeIndex):
                    raise ValueError('ResearchPipeline calendar response must be a DatetimeIndex')
                if group == 'calendar' and not result.data.is_monotonic_increasing:
                    raise ValueError('calendar sessions must be increasing')
                frame = (pd.DataFrame({'date': result.data}) if isinstance(result.data, pd.DatetimeIndex)
                         else result.data.copy())
                receipt['available_fields'] = list(frame.columns)
                if not frame.columns.is_unique or set(FIELDS[group]) - set(frame):
                    raise ValueError('required fields missing or duplicated: ' + ','.join(sorted(set(FIELDS[group]) - set(frame))))
                if 'date' in frame:
                    frame['date'] = _dates(frame.date)
                    if not frame.date.between(qlo, qhi).all():
                        raise ValueError('returned dates outside query')
                if 'symbol' in frame and not frame.symbol.eq(q.symbol).all():
                    raise ValueError('returned security conflicts with query')
                if group == 'listings':
                    frame['listing_date'] = _dates(frame.listing_date)
                    frame['delist_date'] = pd.to_datetime(frame.delist_date)
                    present = frame.delist_date.notna()
                    _dates(frame.loc[present, 'delist_date'])
                    if (frame.loc[present, 'delist_date'] < frame.loc[present, 'listing_date']).any():
                        raise ValueError('delisting precedes listing')
                keys = ['symbol'] if group == 'listings' else [k for k in ('date', 'symbol') if k in frame]
                if frame.duplicated(keys).any():
                    raise ValueError('duplicate observation keys')
                # Identity/structure-checked observations survive numeric rejection.
                observations[group].append(frame.copy())
                receipt['identity_and_structure_checked'] = True
                numeric = NUMERIC.get(group, ())
                for name in numeric:
                    unit = pin.units.get(name)
                    if not unit or 'UNKNOWN' in unit.upper():
                        fail('UNIT_UNDECLARED', group, symbol=q.symbol, field=name)
                    values = pd.to_numeric(frame[name], errors='raise')
                    if not np.isfinite(values.to_numpy(dtype=float)).all():
                        raise ValueError('nonfinite required field: ' + name)
                if group == 'bars':
                    for name in [p + f for p in ('raw_', 'adj_') for f in OHLC]:
                        origin = pin.field_origins.get(name)
                        if not origin or 'UNKNOWN' in origin.upper():
                            fail('PRICE_ORIGIN_UNDECLARED', group, symbol=q.symbol, field=name)
                    for prefix in ('raw_', 'adj_'):
                        o, h, l, c = (frame[prefix + f] for f in OHLC)
                        if ((l <= 0) | (h < l) | (h < o) | (h < c) | (l > o) | (l > c)).any():
                            raise ValueError('invalid price relationship')
                    if frame[['volume', 'amount', 'turnover']].lt(0).any().any():
                        raise ValueError('negative market quantity')
                if group == 'benchmark' and frame.open.le(0).any():
                    raise ValueError('nonpositive benchmark open')
                if group == 'status' and (not frame.trade_status.isin([0, 1]).all() or not frame.is_st.isin([False, True]).all()):
                    raise ValueError('unsupported status encoding')
                if group == 'membership' and 'is_member' in frame:
                    if not frame.is_member.isin([True, False]).all():
                        raise ValueError('invalid membership flag')
                    frame = frame.loc[frame.is_member.eq(True)].copy()
                parts.append(frame)
                receipt['status'] = 'PASS'
            except (FactorLabError, ValueError, TypeError, KeyError, OSError) as exc:
                receipt['reason'] = str(exc)
                fail('INPUT_REJECTED', group, query=asdict(q), reason=str(exc))
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            keys = ['symbol'] if group == 'listings' else [k for k in ('date', 'symbol') if k in frame]
            if frame.duplicated(keys).any():
                fail('OVERLAPPING_QUERY_KEYS', group)
            else:
                frames[group] = frame

    for pin in actions:
        receipt = dict(query=asdict(pin), status='FAIL', known_at='UNKNOWN')
        action_receipts.append(receipt)
        try:
            if not all((pin.source, pin.source_id, pin.version, pin.security_id, pin.content_sha256)):
                raise ValueError('explicit action identity required')
            if _dates([pin.start, pin.end]).tolist() != [lo, hi]:
                raise ValueError('actions must bind the full input window')
            result = hub.read(pin.source, 'corporate_actions', security_id=pin.security_id,
                              start=pin.start, end=pin.end)
            receipt['metadata'] = asdict(result.metadata)
            receipt['content_sha256'] = content_identity(result.data)
            if (result.metadata.source_id != pin.source_id or result.metadata.data_version != pin.version
                    or receipt['content_sha256'] != pin.content_sha256):
                raise ValueError('action source/version/content mismatch')
            if result.metadata.coverage != 'COMPLETE_QUERY_EVENT_COVERAGE':
                raise ValueError('explicit full-window action coverage declaration required, including empty event sets')
            if any(event.security_id != pin.security_id for event in result.data):
                raise ValueError('action security identity mismatch')
            receipt['event_references'] = [dict(security_id=e.security_id, event_id=e.event_id,
                revision=e.revision, source=e.source_id, content_identity=e.source_sha256,
                effective_date=e.effective_date, announced_at=e.announced_at,
                available_at_claim=e.available_at) for e in result.data]
            receipt['status'] = 'PASS'
        except (FactorLabError, ValueError, TypeError, KeyError, OSError) as exc:
            fail('ACTION_INPUT_REJECTED', 'corporate_actions', reason=str(exc))
            receipt['reason'] = str(exc)
    if require_actions:
        listings = frames.get('listings')
        if listings is None or 'security_id' not in listings:
            fail('ACTION_SECURITY_MAPPING_MISSING', 'corporate_actions')
        else:
            required = set(listings.security_id)
            bound = {p.security_id for p, r in zip(actions, action_receipts) if r['status'] == 'PASS'}
            if len(required) != len(symbols) or not required.issubset(bound):
                fail('ACTION_COVERAGE_MISSING', 'corporate_actions')
    matrix, layers, endpoints = [], {}, []
    if 'calendar' in frames:
        calendar = pd.DatetimeIndex(frames['calendar'].date.sort_values())
        research = calendar[(calendar >= first) & (calendar <= last)]
        warmup, tail = calendar[calendar < first], calendar[calendar > last]
        for name, dates in [('warmup', warmup), ('research', research), ('tail', tail)]:
            layers[name] = dict(sessions=len(dates), dates=[str(d.date()) for d in dates])
        if first not in calendar or last not in calendar:
            fail('RESEARCH_BOUNDARY_NOT_SESSION', 'calendar')
        if len(warmup) < warmup_sessions:
            fail('WARMUP_INSUFFICIENT', 'calendar', required=warmup_sessions, observed=len(warmup))
        required_tail = max(holding_days + 1, execution_tail_sessions)
        if len(tail) < required_tail:
            fail('TAIL_INSUFFICIENT', 'calendar', required=required_tail, observed=len(tail))
        for group, frame in frames.items():
            if 'date' in frame and not frame.date.isin(calendar).all():
                fail('OFF_CALENDAR_OBSERVATION', group)
        listing = frames.get('listings', pd.DataFrame(columns=['symbol'])).set_index('symbol')
        for symbol in symbols:
            if symbol not in listing.index:
                fail('LISTING_IDENTITY_MISSING', 'listings', symbol=symbol)
            bars = frames.get('bars')
            observed_warmup = 0 if bars is None else int((bars.symbol.eq(symbol) & bars.date.isin(warmup)).sum())
            if observed_warmup < warmup_sessions:
                fail('SECURITY_WARMUP_INSUFFICIENT', 'bars', symbol=symbol,
                     required=warmup_sessions, observed=observed_warmup)
            for day in calendar:
                exists = 'UNKNOWN'
                if symbol in listing.index:
                    row = listing.loc[symbol]
                    exists = not (day < row.listing_date or (pd.notna(row.delist_date) and day > row.delist_date))
                phase = 'warmup' if day < first else 'tail' if day > last else 'research'
                cell = dict(symbol=symbol, date=str(day.date()), phase=phase,
                    observed_listing_lifetime=exists, historical_eligibility='UNKNOWN')
                for group in ('bars', 'membership', 'status', 'benchmark'):
                    frame = frames.get(group)
                    present = False if frame is None else bool((frame.date.eq(day) &
                        (frame.symbol.eq(symbol) if group != 'benchmark' else True)).any())
                    cell[group] = 'OBSERVED' if present else ('NOT_EXISTING_OBSERVATION' if exists is False and group != 'benchmark' else 'MISSING' if exists is True and group != 'membership' else 'UNKNOWN')
                    needed = group == 'benchmark' or (exists is True and group in {'bars', 'status'})
                    if needed and not present:
                        fail('COVERAGE_MISSING', group, symbol=symbol, date=str(day.date()))
                    if exists is False and present and group != 'benchmark':
                        fail('LIFETIME_CONFLICT', group, symbol=symbol, date=str(day.date()))
                matrix.append(cell)
        if 'membership' in frames:
            members = frames['membership'].loc[frames['membership'].date.isin(research)]
            for symbol in symbols:
                if not members.symbol.eq(symbol).any():
                    fail('SECURITY_MEMBERSHIP_UNKNOWN', 'membership', symbol=symbol)
            for day in research:
                if not members.date.eq(day).any():
                    fail('MEMBERSHIP_DAY_UNKNOWN', 'membership', date=str(day.date()))
            if {'bars', 'benchmark'}.issubset(frames):
                endpoints = label_availability(frames['bars'], members[['date', 'symbol']], calendar,
                    frames['benchmark'].date, holding_days=holding_days).to_dict('records')
                if any(not r['label_endpoints_available'] for r in endpoints):
                    fail('LABEL_ENDPOINT_MISSING', 'bars')
    else:
        fail('CALENDAR_UNAVAILABLE', 'calendar')
    engineering = 'FAIL' if issues else 'PASS'
    return dict(schema='P4_01_PIPELINE_PREFLIGHT_V1', boundary=BOUNDARY,
        data_existence='OBSERVED' if any(r['status'] == 'PASS' for r in receipts) else 'UNKNOWN',
        engineering=engineering, formal_admission='BLOCKED', requested_formal=requested_formal,
        formal_request='REJECTED' if requested_formal else 'NOT_REQUESTED',
        historical_pit='UNKNOWN', hfq_qualification='UNKNOWN',
        query_completeness=completeness, receipts=receipts, action_receipts=action_receipts,
        window=dict(start=start, end=end, research_start=research_start, research_end=research_end,
            warmup_sessions=warmup_sessions, holding_days=holding_days,
            execution_tail_sessions=execution_tail_sessions),
        symbols=list(symbols), layers=layers, coverage_matrix=matrix, label_endpoints=endpoints,
        research_quality=_research_quality(observations, receipts, symbols),
        unmet_contracts=issues, evidence_gaps=['PIT_UNVERIFIED', 'HFQ_UNVERIFIED',
            'Independent historical source, unit and security identity certification',
            'Replayable formal market/execution qualification package'],
        summary_zh=f'工程输入预检：{engineering}；未满足合同 {len(issues)} 项；历史PIT及HFQ资格 UNKNOWN；正式金融准入 BLOCKED。')
