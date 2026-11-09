"""Batch engineering observations; never a financial qualification report."""
from __future__ import annotations

from collections import Counter
from itertools import combinations

import pandas as pd

from factorlab.data.governance import diagnose_tables
from factorlab.diagnostics.candidate_preflight import (
    BOUNDARY, CandidateQuery, RAW_FIELDS, preflight,
)

STATES = frozenset({'PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE'})


class _RecordingHub:
    def __init__(self, hub):
        self.hub = hub
        self.result = None

    def read(self, *args, **kwargs):
        self.result = self.hub.read(*args, **kwargs)
        return self.result


def _gap(query, condition, category, evidence):
    return dict(source=query.source, version=query.version, symbol=query.symbol,
                start=query.start, end=query.end, condition=condition,
                category=category, required_evidence=evidence)


def batch_readiness(hub, queries: tuple[CandidateQuery, ...], *, calendar,
                    calendar_id: str, calendar_kind: str, units: dict,
                    research_inputs: dict | None = None):
    """Use an explicit common grid and independently selected source versions.

    Gap categories are evidence requests, not repair authorization. No code aliases,
    historical status or membership are inferred from an observed price series.
    Cross-source comparisons reuse exactly the reads diagnosed by preflight.
    """
    if not queries or len({(q.source, q.symbol) for q in queries}) != len(queries):
        raise ValueError('nonempty unique source/security queries required')
    if len({(q.start, q.end, q.research_start, q.research_end, q.holding_days)
            for q in queries}) != 1:
        raise ValueError('one explicit batch window and label horizon required')
    dates = pd.DatetimeIndex(calendar)
    reports, results, gaps = [], [], []
    for query in queries:
        recording = _RecordingHub(hub)
        diagnostic = preflight(recording, query, calendar=dates,
            calendar_id=calendar_id, calendar_kind=calendar_kind, units=units)
        report = diagnostic.to_dict()
        # Keep the legacy preflight API unchanged; the batch evidence schema has
        # four states. Unexecuted checks have no evidence, not failed conditions.
        for check in report['checks'].values():
            if check['status'] not in STATES:
                check['status'] = 'UNKNOWN'
        for check in [*report['factors'], report['labels']]:
            if check['status'] not in STATES:
                check['preflight_outcome'] = check['status']
                check['status'] = 'UNKNOWN'
        if diagnostic.accessibility != 'ACCESSIBLE':
            report['gates'] = {name: {'status': 'UNKNOWN'} for name in
                ('RAW', 'ADJUSTMENT', 'STATUS', 'PIT', 'SOURCE_BINDING')}
            if report['checks'].get('input', {}).get('status') == 'FAIL':
                report['gates']['RAW'] = {'status': 'FAIL', 'reason': 'DataHub rejected the input'}
            report['gates']['FORMAL'] = {'status': 'FAIL', 'reason': 'FORMAL_ADMISSION_BLOCKED'}
            gaps.append(_gap(query, 'RAW', 2, 'Accessible selected source/version and original bytes'))
        else:
            results.append((query, recording.result))
            checks = report['checks']
            raw_names = ('coverage', 'duplicates', 'missing', 'ohlc', 'anomalies')
            raw_status = 'FAIL' if any(checks[n]['status'] == 'FAIL' for n in raw_names) else 'PASS'
            report['gates'] = {
                'RAW': dict(status=raw_status, scope='ENGINEERING_ONLY', checks=list(raw_names)),
                'ADJUSTMENT': dict(status='UNKNOWN', reason='HFQ_UNVERIFIED'),
                'STATUS': dict(status='UNKNOWN', reason='No bound historical ST/suspension/listing/delisting evidence'),
                'PIT': dict(status='UNKNOWN', reason='No bound membership knowledge times or historical revision chain'),
                'SOURCE_BINDING': dict(status='UNKNOWN', reason='Content identity is not independent upstream certification'),
                'FORMAL': dict(status='FAIL', reason='FORMAL_ADMISSION_BLOCKED'),
            }
            if raw_status == 'FAIL':
                gaps.append(_gap(query, 'RAW', 4,
                    'Establish historical eligibility and null/absence semantics first; only then request missing original observations. Pre-listing and post-delisting prices must not be manufactured'))
            gaps.extend([
                _gap(query, 'PIT', 3, 'Historical payload vintages, first-distribution timestamps/timezones and replacement/retraction chain; membership and status included'),
                _gap(query, 'ADJUSTMENT', 4, 'All-category official action coverage, implementation/revision originals, independent reference, algorithm and absolute anchor'),
                _gap(query, 'STATUS', 4, 'Independent historical ST/suspension/listing/delisting and official session evidence'),
                _gap(query, 'SOURCE_BINDING', 4, 'Independent scoped identity, economic units and upstream-to-normalized source contracts'),
                _gap(query, 'FORMAL', 5, 'Formal review after evidence and existing deterministic parser contracts are complete; not repairable by threshold changes'),
            ])
        reports.append(report)
    comparisons = []
    for (left_q, left), (right_q, right) in combinations(results, 2):
        if left_q.symbol != right_q.symbol or left_q.source == right_q.source:
            continue
        fields = [*RAW_FIELDS, 'volume', 'amount']
        conflicts = diagnose_tables(left, right, source=left_q.source,
            version=left_q.version, other_source=right_q.source,
            other_version=right_q.version, fields=fields)
        comparisons.append(dict(symbol=left_q.symbol, start=left_q.start, end=left_q.end,
            left_source=left_q.source, left_version=left_q.version,
            right_source=right_q.source, right_version=right_q.version,
            status='FAIL' if conflicts else 'PASS', scope='EXACT_OBSERVED_VALUES_ONLY',
            semantic_qualification='UNKNOWN', conflicts=len(conflicts),
            by_field=dict(Counter(c['field'] for c in conflicts)),
            by_type=dict(Counter(c['type'] for c in conflicts)), merged=False))
        if conflicts:
            gaps.append(_gap(left_q, 'CROSS_SOURCE', 4,
                'Independent matching unit/precision/field definitions for both versions; retain differences, never select by priority'))
    research_reports = {}
    if research_inputs:
        from factorlab.diagnostics.research_readiness import research_readiness
        for source, options in research_inputs.items():
            selected = [q for q in queries if q.source == source]
            if not selected:
                raise ValueError('research source absent from batch queries')
            query = selected[0]
            if query.holding_days != 5:
                raise ValueError('D06 research diagnostics require the existing five-session label horizon')
            market = options['inputs'].get('bars')
            if market is not None:
                version = market.metadata.provenance.get('governance_version', market.metadata.data_version)
                if version != query.version:
                    raise ValueError('research bars version differs from batch version')
                observed_sources = {result.metadata.source_id for q, result in results if q.source == source}
                if observed_sources != {market.metadata.source_id}:
                    raise ValueError('research bars source differs from batch source')
                if not market.data.symbol.isin([q.symbol for q in selected]).all():
                    raise ValueError('research bars contain unselected securities')
            research_reports[source] = research_readiness(**options,
                start=query.start, end=query.end,
                research_start=query.research_start, research_end=query.research_end)
            for report in reports:
                if report['query']['source'] == source:
                    bindings = research_reports[source]['bindings']
                    if {'status', 'listings'}.issubset(bindings):
                        report['gates']['STATUS'] = dict(status='UNKNOWN',
                            reason='Observed historical status/listing sources bound; independent historical qualification UNKNOWN')
                    if 'membership' in bindings:
                        report['gates']['PIT'] = dict(status='UNKNOWN',
                            reason='Observed member-day source bound; historical known_at and certified stable identity UNKNOWN')
    return dict(schema='P3D07_READINESS_V1', status='PARTIAL', boundary=BOUNDARY,
        research_diagnostics=research_reports,
        calendar=dict(id=calendar_id, kind=calendar_kind, sessions=len(dates),
                      historical_qualification='UNKNOWN'),
        sources=reports, comparisons=comparisons, gaps=gaps,
        research_window=dict(status='UNKNOWN', qualified_window=None,
            reason='No financially qualified historical window; shared D06 engineering results are separate',
            original14='UNKNOWN', candidates64='UNKNOWN',
            label_values_computed=False),
        gap_categories={1: 'LOCAL_PARSE_OR_MAPPING', 2: 'ACTUAL_SOURCE_RETRIEVAL_REQUIRED',
            3: 'VENDOR_TIME_OR_REVISION_EVIDENCE', 4: 'OFFICIAL_OR_INDEPENDENT_EVIDENCE',
            5: 'UNRESOLVED_UNDER_CURRENT_CONDITIONS'})
