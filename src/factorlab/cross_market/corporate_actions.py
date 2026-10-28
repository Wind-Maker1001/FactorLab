"""Evidence-aware action ledger. Observed source rows are not completeness proof.

Coverage intervals have three states. Missing intervals and evidence unavailable
at an execution instant remain UNKNOWN. Known states require explicit verified
evidence, source version, and an aware availability timestamp. The class validates
these assertions; it cannot authenticate a supplier or create a trust root.
"""
import pandas as pd
from .adapters import ActionLedger
from .contracts import ContractError, instant, session_date


class EvidenceActionLedger(ActionLedger):
    STATES = {'VERIFIED_NO_EVENT', 'VERIFIED_EVENT', 'UNKNOWN'}
    COVERAGE_COLUMNS = ['instrument_id', 'start', 'end', 'state', 'source_version',
                        'available_at', 'evidence_status', 'evidence']

    def __init__(self, events, coverage, *, version):
        if set(self.COVERAGE_COLUMNS) - set(coverage) or coverage.empty:
            raise ContractError('explicit three-state action coverage required')
        c = coverage.copy()
        c['start'] = pd.to_datetime([session_date(v) for v in c.start])
        c['end'] = pd.to_datetime([session_date(v) for v in c.end])
        if not set(c.state) <= self.STATES or (c.start > c.end).any() or c.instrument_id.isna().any():
            raise ContractError('invalid action coverage state or interval')
        c['available_at'] = pd.to_datetime(c.available_at, utc=True)
        for r in c.itertuples():
            if r.state != 'UNKNOWN':
                if r.evidence_status != 'VERIFIED' or not isinstance(r.evidence, str) or not r.evidence or not isinstance(r.source_version, str) or not r.source_version or pd.isna(r.available_at):
                    raise ContractError('known coverage requires verified evidence, source version and availability')
                # Check the original value, since pd.to_datetime(utc=True)
                # would otherwise silently interpret a naive value as UTC.
                instant(coverage.loc[r.Index, 'available_at'])
        for _, f in c.groupby('instrument_id'):
            f = f.sort_values('start')
            if (f.start.iloc[1:].to_numpy() <= f.end.iloc[:-1].to_numpy()).any():
                raise ContractError('overlapping action coverage')
        self.coverage = c
        extra = {'source_version', 'evidence_status'}
        if extra - set(events):
            raise ContractError('event source version and evidence status required')
        if not events.empty:
            if not events.evidence_status.eq('VERIFIED').all() or not events.source_version.map(lambda x:isinstance(x,str) and bool(x)).all():
                raise ContractError('unverified candidate event is not executable')
        super().__init__(events, coverage_start=c.start.min(), coverage_end=c.end.max(),
                         instrument_ids=c.instrument_id.unique(), version=version)
        for r in self.frame.itertuples():
            if r.kind in {'merger', 'delisting'} and r.cash_per_share > 0 and (pd.isna(r.pay_date) or r.pay_date != r.effective_date):
                raise ContractError('CORPORATE_ACTION_DEFERRED_CONSIDERATION_UNKNOWN: explicit payment accounting required')
            rows = c.loc[c.instrument_id.eq(r.instrument_id) & c.start.le(r.effective_date) & c.end.ge(r.effective_date)]
            if len(rows) != 1 or rows.state.iloc[0] != 'VERIFIED_EVENT':
                raise ContractError('event conflicts with unknown/no-event coverage')
        for r in c.loc[c.state.eq('VERIFIED_EVENT')].itertuples():
            if self.frame.loc[self.frame.instrument_id.eq(r.instrument_id) & self.frame.effective_date.between(r.start,r.end)].empty:
                raise ContractError('verified-event coverage has no event')

    def coverage_state(self, instrument_id, day, as_of):
        day, as_of = session_date(day), instant(as_of)
        f = self.coverage
        f = f.loc[f.instrument_id.eq(instrument_id) & f.start.le(day) & f.end.ge(day)]
        if len(f) != 1 or pd.isna(f.available_at.iloc[0]) or f.available_at.iloc[0] > as_of:
            return 'UNKNOWN'
        return f.state.iloc[0]

    def require_coverage(self, start, end, instrument_ids):
        start, end = session_date(start), session_date(end)
        for iid in set(instrument_ids):
            cursor = start
            f = self.coverage.loc[self.coverage.instrument_id.eq(iid) & self.coverage.end.ge(start) & self.coverage.start.le(end)].sort_values('start')
            for r in f.itertuples():
                if r.start > cursor or r.state == 'UNKNOWN':
                    raise ContractError('CORPORATE_ACTION_COVERAGE_UNKNOWN: ' + iid)
                cursor = r.end + pd.Timedelta(days=1)
            if cursor <= end:
                raise ContractError('CORPORATE_ACTION_COVERAGE_UNKNOWN: ' + iid)

    def require_session(self, day, instrument_ids, as_of):
        for iid in set(instrument_ids):
            if self.coverage_state(iid, day, as_of) == 'UNKNOWN':
                raise ContractError('CORPORATE_ACTION_COVERAGE_UNKNOWN_AT_EXECUTION: ' + iid)
