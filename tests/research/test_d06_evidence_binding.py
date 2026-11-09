"""Evidence envelopes cannot promote retrospective observations to PIT facts."""
from dataclasses import replace

import pandas as pd

from factorlab.data.corporate_actions.contracts import CorporateAction, SourceSnapshot
from factorlab.data.hub import DataHub
from factorlab.data.hub.adapters import CorporateActionAdapter
from factorlab.data.hub.contracts import QueryResult, SourceMetadata
from factorlab.diagnostics.research_readiness import research_readiness


def test_existing_action_adapter_retains_revision_and_time_references(tmp_path):
    from factorlab.data.evidence import snapshot_file
    path = tmp_path / 'action.json'
    path.write_text('synthetic action source')
    artifact = snapshot_file(path, 'FIXTURE', '2026-02-01T00:00:00+00:00')
    event = CorporateAction(security_id='fixture-stable-id', event_id='fixture-event', revision=1,
        event_type='CASH_DIVIDEND', effective_date='2021-09-15',
        announced_at='2021-09-01T00:00:00+00:00', available_at='2026-01-01T00:00:00+00:00',
        source_id='FIXTURE', source_sha256=artifact.sha256, synthetic_only=True)
    snapshot = SourceSnapshot('FIXTURE', 'v1', '2026-02-01T00:00:00+00:00',
        (event,), (), artifacts=(artifact,), synthetic_only=True)
    result = DataHub({'actions': CorporateActionAdapter(snapshot)}).read('actions', 'corporate_actions',
        security_id='fixture-stable-id', start='2021-09-01', end='2021-09-30')
    report = research_readiness({'corporate_actions': result},
        identities={'corporate_actions': ('FIXTURE', 'v1')},
        start='2021-09-01', end='2021-09-30', research_start='2021-09-01', research_end='2021-09-30')
    binding = report['bindings']['corporate_actions']
    assert binding['event_vintages'] == 1
    assert binding['event_references'][0]['content_identity'] == event.source_sha256
    assert binding['event_references'][0]['revision'] == 1
    assert binding['event_references'][0]['available_at_claim'] == event.available_at
    assert binding['event_references'][0]['known_at'] == 'UNKNOWN'
    assert report['historical_identity'] == 'UNKNOWN'
    assert report['formal_admission'] == 'FAIL'


def test_bound_missing_status_is_not_healthy_and_short_windows_are_unknown():
    dates = pd.bdate_range('2021-01-04', periods=90)
    bars = pd.DataFrame(dict(date=dates, symbol='600000.SH', volume=100., amount=1000., turnover=.01))
    for prefix in ('raw_', 'adj_'):
        for field in ('open', 'high', 'low', 'close'):
            bars[prefix + field] = 10.
    frames = dict(bars=bars, calendar=pd.DataFrame(dict(date=dates)),
        membership=bars.loc[65:, ['date', 'symbol']],
        benchmark=pd.DataFrame(dict(date=dates, open=100.)),
        status=bars[['date', 'symbol']].assign(trade_status=float('nan'), is_st=float('nan')),
        listings=pd.DataFrame(dict(symbol=['600000.SH'], listing_date=[pd.Timestamp('2010-01-01')])))
    inputs = {kind: QueryResult(frame, SourceMetadata('FIXTURE', 'v1', provenance={'source_kind': 'SYNTHETIC'}))
        for kind, frame in frames.items()}
    report = research_readiness(inputs, identities={kind: ('FIXTURE', 'v1') for kind in inputs},
        start=str(dates[0].date()), end=str(dates[-1].date()),
        research_start=str(dates[65].date()), research_end=str(dates[-1].date()))
    assert report['windows'] == []
    assert all(row['status'] == 'UNKNOWN' for row in report['window_candidates'])
    assert all(row['null_on_present_keys'] == 25 for row in report['status_observations'])
    assert report['historical_identity'] == 'UNKNOWN'
    assert report['formal_admission'] == 'FAIL'


def test_governance_scoped_query_keeps_capture_identity_without_false_hash_rejection(tmp_path):
    from factorlab.data.evidence import snapshot_file, table_sha256
    from factorlab.data.governance import GovernanceStore
    path = tmp_path / 'source.txt'
    path.write_text('synthetic source capture')
    artifact = snapshot_file(path, 'FIXTURE', '2026-01-01T00:00:00+00:00')
    frame = pd.DataFrame(dict(date=pd.bdate_range('2021-01-04', periods=3), symbol='600000.SH', raw_open=10.))

    class Adapter:
        capabilities = frozenset({'daily_bars'})

        def read(self, kind, **kwargs):
            return QueryResult(frame.copy(), SourceMetadata('FIXTURE', 'vendor-v1',
                provenance={'references': [{'source_sha256': artifact.sha256}]}))

    store = GovernanceStore(tmp_path / 'governance')
    store.capture(DataHub({'fixture': Adapter()}), 'fixture', 'daily_bars',
        version='capture-v1', symbol='600000.SH', start='2021-01-04', end='2021-01-06',
        artifacts=(artifact,), acquired_at='2026-01-01T00:00:00+00:00')
    selected = store.read('fixture', 'daily_bars', version='capture-v1', symbol='600000.SH',
        start='2021-01-05', end='2021-01-05')
    assert selected.metadata.provenance['table_sha256'] != table_sha256(selected.data)
    report = research_readiness({'bars': selected}, identities={'bars': ('FIXTURE', 'capture-v1')},
        start='2021-01-05', end='2021-01-05', research_start='2021-01-05', research_end='2021-01-05')
    assert report['bindings']['bars']['version'] == 'capture-v1'
    assert report['bindings']['bars']['content_identity'] == table_sha256(selected.data)
    assert report['bindings']['bars']['known_at'] == 'UNKNOWN'
