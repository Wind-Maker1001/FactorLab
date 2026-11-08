"""Retrospective RAW diagnostics, deliberately separate from financial admission."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

import numpy as np
import pandas as pd

from factorlab.data.hub import DataHub
from factorlab.data.evidence import table_sha256
from factorlab.errors import DataQualityError
from factorlab.factors.registry_v2 import FactorDataContext, FactorDefinition, FactorRegistryV2
from factorlab.exploratory.factor_discovery.contracts import FIELDS, Node
from factorlab.exploratory.factor_discovery.engine import compile_expression

BOUNDARY = 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED'
RAW_FIELDS = tuple('raw_' + name for name in ('open', 'high', 'low', 'close'))


class CheckStatus(str, Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    UNKNOWN = 'UNKNOWN'
    NOT_AVAILABLE = 'NOT_AVAILABLE'
    NOT_RUN = 'NOT_RUN'


@dataclass(frozen=True)
class CandidateQuery:
    source: str
    version: str
    symbol: str
    start: str
    end: str
    research_start: str
    research_end: str
    holding_days: int = 5

    def __post_init__(self):
        if not self.version.strip() or not self.source.strip():
            raise ValueError('explicit source and version required')
        if not (pd.Timestamp(self.start) <= pd.Timestamp(self.research_start)
                <= pd.Timestamp(self.research_end) <= pd.Timestamp(self.end)):
            raise ValueError('research window must lie inside input window')
        if type(self.holding_days) is not int or self.holding_days < 1:
            raise ValueError('positive integer holding_days required')


@dataclass(frozen=True)
class CandidateDataDiagnostics:
    """Aggregate observations only; not a provider or qualification object."""
    query: CandidateQuery
    accessibility: str
    source: dict
    checks: dict
    factors: tuple[dict, ...]
    labels: dict
    blocking_evidence: tuple[str, ...]
    boundary: str = BOUNDARY

    def to_dict(self):
        return asdict(self)


def _check(status, **observations):
    return dict(status=CheckStatus(status).value, **observations)


def raw_definitions():
    """Pinned RAW expressions; legacy adjusted-price builtins remain unchanged."""
    close = Node('Field', value='raw_close')
    definitions = []
    for lag in (5, 20, 60):
        expression = Node('Sub', (Node('SafeDiv', (close, Node('Lag', (close,), param=lag))),
                                  Node('Const', value=1.0)))
        definitions.append(FactorDefinition.from_expression(
            'RAW_MOM%d' % lag, '1', compile_expression(expression)))
    expression = Node('Sub', (Node('SafeDiv', (close, Node('RollingMean', (close,), param=20))),
                              Node('Const', value=1.0)))
    definitions.append(FactorDefinition.from_expression('RAW_PRICE_MA20', '1', compile_expression(expression)))
    definitions.append(FactorDefinition.builtin('VOLUME_RATIO'))
    return tuple(definitions)


def label_boundaries(bars, calendar, query):
    """Describe existing next-open label offsets, never calculate returns.

    Exit open is observable at t+h+1, not t+h. Historical availability remains
    unknown even when that observation exists in today's retrospective table.
    """
    dates = pd.DatetimeIndex(calendar)
    research = dates[(dates >= query.research_start) & (dates <= query.research_end)]
    positions = dates.get_indexer(research)
    entry_positions, exit_positions = positions + 1, positions + query.holding_days + 1
    observed = bars.set_index('date')
    complete = []
    for entry, exit_ in zip(entry_positions, exit_positions):
        complete.append(exit_ < len(dates) and all(
            date in observed.index and np.isfinite(observed.at[date, 'raw_open'])
            and observed.at[date, 'raw_open'] > 0
            for date in (dates[entry], dates[exit_])))
    mature_dates = research[np.asarray(complete, dtype=bool)]
    return dict(status='PASS', purpose='RETROSPECTIVE_UNCERTIFIED_BOUNDARIES_ONLY',
        implementation='factorlab.labels.forward_return.build_forward_excess_return_labels',
        holding_days=query.holding_days, entry_offset=1, exit_offset=query.holding_days + 1,
        research_observations=len(research), raw_future_coverage=int(sum(complete)),
        incomplete_future_coverage=int(len(research) - sum(complete)),
        last_signal_with_observed_exit=str(mature_dates[-1].date()) if len(mature_dates) else None,
        last_observed_exit=str(dates[exit_positions[np.flatnonzero(complete)[-1]]].date()) if any(complete) else None,
        historical_maturity='UNKNOWN', provider_first_distribution='UNKNOWN',
        revisions='UNKNOWN', adjusted_open_and_benchmark='NOT_AVAILABLE',
        label_generation='NOT_RUN', company_action_effect='UNVERIFIED',
        note='Observed exit date is not proof of historically available label version.')


def preflight(hub: DataHub, query: CandidateQuery, *, calendar, calendar_id: str,
              calendar_kind: str, units: dict, identity_evidence: str | None = None,
              action_evidence: dict | None = None) -> CandidateDataDiagnostics:
    """Read exactly one source/version. Quality PASS never grants admission.

    Calendar is an explicit diagnostic grid, not inferred official sessions.
    Unit descriptions and identity references are caller evidence, not approval.
    """
    dates = pd.DatetimeIndex(calendar)
    if (dates.empty or dates.hasnans or dates.has_duplicates or dates.tz is not None
            or not dates.is_monotonic_increasing or not dates.equals(dates.normalize())
            or (dates < query.start).any() or (dates > query.end).any()):
        raise ValueError('explicit sorted unique daily calendar inside input window required')
    try:
        result = hub.read(query.source, 'daily_bars', symbol=query.symbol,
                          start=query.start, end=query.end)
    except (FileNotFoundError, PermissionError) as exc:
        return CandidateDataDiagnostics(query, 'NOT_ACCESSIBLE', {},
            {'input': _check('NOT_RUN', reason=type(exc).__name__)}, (),
            {'status': 'NOT_RUN'}, ('SOURCE_NOT_ACCESSIBLE',))
    except DataQualityError as exc:
        return CandidateDataDiagnostics(query, 'ACCESSIBLE_READ_REJECTED', {},
            {'input': _check('FAIL', reason=str(exc))}, (),
            {'status': 'NOT_RUN'}, ('DATAHUB_REJECTED_INPUT',))
    selected_version = result.metadata.provenance.get('governance_version', result.metadata.data_version)
    if selected_version != query.version:
        raise ValueError('DataHub version differs from explicitly selected version')
    frame = result.data.copy()
    required = {'date', 'symbol', *RAW_FIELDS, 'volume'}
    if not required.issubset(frame):
        raise ValueError('candidate missing required columns: ' + str(sorted(required - set(frame))))
    frame['date'] = pd.to_datetime(frame.date)
    if not frame.symbol.eq(query.symbol).all() or not frame.date.between(query.start, query.end).all():
        raise ValueError('source returned rows outside requested security/window')
    if frame.date.isna().any() or not frame.date.equals(frame.date.dt.normalize()) or frame.date.dt.tz is not None:
        raise ValueError('candidate dates must be timezone-naive daily keys')
    numeric = frame[[*RAW_FIELDS, 'volume'] + (['amount'] if 'amount' in frame else [])]
    missing = {name: int(values.isna().sum()) for name, values in numeric.items()}
    illegal = {name: int(np.isinf(values.to_numpy(dtype=float)).sum()) for name, values in numeric.items()}
    o, h, l, c = (frame[name] for name in RAW_FIELDS)
    order = int(((h < l) | (h < o) | (h < c) | (l > o) | (l > c)).sum())
    negative = int((frame[list(RAW_FIELDS)] <= 0).any(axis=1).sum())
    negative_flow = int((numeric[[n for n in ('volume', 'amount') if n in numeric]] < 0).any(axis=1).sum())
    missing_dates = dates.difference(pd.DatetimeIndex(frame.date))
    outside = pd.DatetimeIndex(frame.date).difference(dates)
    metadata = asdict(result.metadata)
    metadata.update(table_sha256=table_sha256(frame), symbol=query.symbol,
                    identity_evidence=identity_evidence, field_units=dict(units),
                    diagnostic_calendar_id=calendar_id, diagnostic_calendar_kind=calendar_kind)
    actions = dict(action_evidence or {})
    checks = dict(
        coverage=_check('FAIL' if len(missing_dates) or len(outside) else 'PASS',
            rows=len(frame), unique_security_dates=len(frame.drop_duplicates(['date', 'symbol'])),
            missing_sessions=len(missing_dates), outside_calendar=len(outside),
            warmup_rows=int((frame.date < query.research_start).sum()),
            research_rows=int(frame.date.between(query.research_start, query.research_end).sum()),
            first=str(frame.date.min().date()) if len(frame) else None,
            last=str(frame.date.max().date()) if len(frame) else None),
        duplicates=_check('PASS', excess=0),
        missing=_check('FAIL' if any(missing.values()) else 'PASS', counts=missing),
        ohlc=_check('FAIL' if order or negative else 'PASS', order_violations=order, nonpositive_prices=negative),
        anomalies=_check('FAIL' if any(illegal.values()) or negative_flow else 'PASS',
                         nonfinite_nonnull=illegal, negative_flow_rows=negative_flow,
                         economic_outliers='UNKNOWN'),
        units=_check('UNKNOWN', evidence=units, reason='Descriptions require vendor contract; not independently certified'),
        identity=_check('UNKNOWN', evidence=identity_evidence),
        calendar=_check('UNKNOWN', calendar_id=calendar_id, kind=calendar_kind),
        time_fields=_check('UNKNOWN', effective_at='date',
            provider_available_at=result.metadata.provider_available_at,
            acquired_at=result.metadata.acquired_at, first_distribution='UNKNOWN', revisions='UNKNOWN'),
        corporate_actions=_check('UNKNOWN', evidence=actions,
            reason='Candidate counts or empty observations do not prove all-category coverage'),
        pit=_check('UNKNOWN', evidence='PIT_UNVERIFIED'),
        hfq=_check('UNKNOWN', evidence='HFQ_UNVERIFIED'),
        formal_admission=_check('FAIL', evidence='FORMAL_ADMISSION_BLOCKED'))
    factors = []
    can_compute = not (order or negative or negative_flow or any(illegal.values()) or len(outside) or frame.empty)
    registry = FactorRegistryV2()
    definitions = raw_definitions()
    registry.register_many(definitions)
    # The expression engine requires its complete whitelist. Unknown unused fields
    # remain NaN, never raw-as-adjusted substitutes or zero-filled market values.
    panel = frame[['date', 'symbol', *RAW_FIELDS, 'volume']
                  + (['amount'] if 'amount' in frame else [])].copy()
    for field in FIELDS:
        if field != 'RET1' and field not in panel:
            panel[field] = np.nan
    panel = panel.reindex(columns=['date', 'symbol'] + [field for field in FIELDS if field != 'RET1'])
    research_dates = dates[(dates >= query.research_start) & (dates <= query.research_end)]
    for definition in definitions:
        summary = dict(definition=asdict(definition), source=query.source, version=query.version,
            symbol=query.symbol, data_sha256=metadata['table_sha256'], identity_evidence=identity_evidence,
            input_window=[query.start, query.end], calculation_window=[query.research_start, query.research_end],
            required_prior_sessions=definition.lookback, denominator=len(research_dates))
        if can_compute:
            context = FactorDataContext(query.source + '@' + query.version, 'RETROSPECTIVE_UNCERTIFIED')
            values = registry.compute(panel, (definition.ref,), calendar=dates, context=context)
            series = values.set_index('date')[definition.factor_id].reindex(research_dates)
            summary.update(status='PASS', valid_samples=int(np.isfinite(series).sum()),
                missing_rate=float(series.isna().mean()) if len(series) else None,
                illegal_values=int(np.isinf(series).sum()), context=asdict(context))
        else:
            summary.update(status='NOT_RUN', reason='Invalid or empty market input')
        factors.append(summary)
    for name in ('MOM20', 'VOL20', 'PRICE_MA20', 'AMIHU20', 'TURN20'):
        definition = FactorDefinition.builtin(name)
        factors.append(dict(definition=asdict(definition), status='NOT_AVAILABLE',
            reason='Certified adjusted prices or qualified turnover unavailable; no RAW substitution',
            source=query.source, version=query.version, symbol=query.symbol))
    labels = label_boundaries(frame, dates, query) if len(frame) else {'status': 'NOT_RUN'}
    blockers = ('HISTORICAL_FIRST_DISTRIBUTION_UNKNOWN', 'REVISION_CHAIN_UNKNOWN',
        'HISTORICAL_IDENTITY_UNVERIFIED', 'INDEPENDENT_CALENDAR_UNVERIFIED',
        'ALL_CATEGORY_ACTION_COVERAGE_UNKNOWN', 'HFQ_ALGORITHM_ANCHOR_UNVERIFIED',
        'FORMAL_ADMISSION_BLOCKED')
    return CandidateDataDiagnostics(query, 'ACCESSIBLE', metadata, checks, tuple(factors), labels, blockers)
