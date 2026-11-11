"""Issuer ST evidence and acceptance at the next unqualified close."""
import json

import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester
from factorlab.data import market_evidence_sidecar as sidecar
from factorlab.data.evidence import sha256
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.oos_execution_recovery import apply_issuer_execution_st
from factorlab.research.oos_execution_study import execution_bars
from tools.research.run_p9_d05 import continue_fold


SYMBOL = '000540.SZ'


@pytest.fixture
def issuer_state(tmp_path, monkeypatch):
    notice, manifest = b'issuer designation', b'frozen source manifest'
    notice_hash = sha256(notice)
    monkeypatch.setattr(sidecar, 'ST_EXECUTION_NOTICE_SHA256', notice_hash)

    def load(*days, published='2023-04-29'):
        rows = [dict(target_security=SYMBOL, target_date=day, field='is_st', original_value=None,
            candidate_value=True, evidence_class='ISSUER_RISK_DESIGNATION', source_uri='test://2023-36',
            source_document_sha256=notice_hash, source_publication_time=published,
            known_at_status=sidecar.ST_EXECUTION_KNOWN, retrospectively_verified_at='2026-11-12',
            verification_status='DOCUMENT_CONFIRMED', consumption_scope='RESEARCH_CANDIDATE_ONLY',
            limitation='Execution status only') for day in days]
        path = tmp_path / 'sidecar.json'
        path.write_text(json.dumps(dict(schema=sidecar.SCHEMA, version=1, safety=list(sidecar.SAFETY),
            source_manifest_sha256=sha256(manifest), candidates=rows)), encoding='utf-8')
        return sidecar.load_sidecar(path, source_manifest=manifest,
            originals={(SYMBOL, day, 'is_st'): None for day in days}, documents={notice_hash: notice})

    return load


def test_issuer_state_requires_exact_sessions_and_as_of_cutoff(issuer_state):
    days = sidecar.ST_EXECUTION_DATES
    evidence = issuer_state(*days)
    for day in days:
        assert evidence.fact(SYMBOL, day, 'is_st', purpose='EXECUTION_STATE',
                             cutoff=f'{day}T09:30:00+08:00').candidate_value is True
    for day in ('2023-05-04', '2023-05-12', '2023-05-19'):
        with pytest.raises(DataQualityError, match='out-of-scope'):
            issuer_state(day)
    with pytest.raises(DataQualityError, match='exact 000540 issuer notice'):
        issuer_state(days[0], published='2023-05-16')
    with pytest.raises(DataQualityError, match='cannot authorize'):
        evidence.fact(SYMBOL, days[0], 'is_st', purpose='EXECUTION_STATE', cutoff='2023-04-28')
    for purpose in ('SIGNAL_STATE', 'EXECUTION'):
        with pytest.raises(DataQualityError, match='cannot authorize'):
            evidence.fact(SYMBOL, days[0], 'is_st', purpose=purpose, cutoff=days[0])


@pytest.mark.parametrize('suspended,reason', [(0, 'sell_price_limit'), (1, 'suspended'), (None, None)])
def test_issuer_status_preserves_native_state_and_order_rules(issuer_state, suspended, reason):
    dates = pd.to_datetime(['2023-05-11', '2023-05-12', '2023-05-15'])
    bars = pd.DataFrame(dict(date=dates, symbol=SYMBOL, raw_open=[1., 1., .95], raw_close=[1., 1., .95],
        volume=10000, suspended=[0, 0, suspended], st_status=[0, 1, 2], is_risk_warning=[0, 1, 1]))
    other = bars.iloc[[-1]].assign(symbol='000046.SZ')
    source = pd.concat([bars, other], ignore_index=True)
    adapted = apply_issuer_execution_st(execution_bars(source), issuer_state('2023-05-15'))
    pd.testing.assert_frame_equal(adapted[source.columns], source, check_exact=True)
    assert adapted.loc[adapted.symbol.eq('000046.SZ'), 'is_st'].isna().all()
    assert adapted.loc[adapted.symbol.eq(SYMBOL) & adapted.date.eq(dates[-1]), 'is_st'].item() == 1
    targets = pd.DataFrame(dict(date=dates[:2], symbol=SYMBOL, target_weight=[.1, 0.]))
    benchmark = pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.))
    engine = AShareExecutionBacktester()
    if reason is None:
        with pytest.raises(SchemaError, match='execution status is missing for 2023-05-15/000540.SZ'):
            engine.run_target_weights(targets, adapted, benchmark)
    else:
        result = engine.run_target_weights(targets, adapted, benchmark)
        assert result.rejections.reason.tolist() == [reason]
        assert result.trades.execution_date.lt(dates[-1]).all()


def test_new_fill_on_unqualified_close_day_cannot_enter_accepted_ledger():
    dates = pd.to_datetime(['2023-05-17', '2023-05-18', '2023-05-19'])
    other = '000541.SZ'
    bars = pd.DataFrame(dict(date=dates.repeat(2), symbol=[SYMBOL, other] * 3,
        raw_open=1., raw_high=1., raw_low=1., raw_close=1., volume=10000, suspended=0, st_status=1))
    halted = bars.symbol.eq(SYMBOL) & bars.date.eq(dates[-1])
    bars.loc[halted, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = float('nan')
    bars.loc[halted, ['volume', 'suspended', 'st_status']] = [0, 1, 2]
    bars = execution_bars(bars)
    benchmark = pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.))
    session = AShareExecutionBacktester().start_session(bars, benchmark, signal_date=dates[0])
    session.submit_targets({SYMBOL: .1})
    session.advance()
    prior = session.result
    evidence = sidecar.MarketEvidence((), sha256(b'manifest'))
    plan = {dates[1]: {SYMBOL: .1, other: .1}}
    accepted, marks, blocker = continue_fold(session, dates[2:], plan, bars, evidence, {}, prior)

    assert session.result.trades.execution_date.eq(dates[-1]).any()
    assert blocker['date'] == '2023-05-19' and 'UNQUALIFIED_HELD_CLOSE' in blocker['reason']
    assert blocker['prior_000540_shares'] > 0
    assert not marks
    assert accepted.nav.date.tolist() == [dates[1]]
    assert accepted.trades.execution_date.eq(dates[1]).all()
    assert accepted.positions.date.eq(dates[1]).all()
