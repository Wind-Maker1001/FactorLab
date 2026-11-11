"""Conditional valuation uses the shared account, never executable prices."""
from dataclasses import replace

import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester
from factorlab.backtest.scenario import ScenarioExecutionSession, ScenarioValuation
from factorlab.errors import SchemaError
from factorlab.research.oos_execution_study import execution_bars
from factorlab.data.market_evidence_sidecar import MarketEvidence
from factorlab.research.oos_execution_recovery import qualify_valuations
from tools.research.run_p9_s01 import definitions, consume
from factorlab.strategies.real_shared_portfolio import audit_ledger

SYMBOL = '000540.SZ'


def account(spec=None, *, initial_other_weight=0., boundary='2023-06-15'):
    dates = pd.to_datetime(['2023-05-17', '2023-05-18', '2023-05-19', '2023-05-22', boundary])
    bars = pd.DataFrame(dict(date=dates.repeat(2), symbol=[SYMBOL, '000541.SZ']*len(dates),
        raw_open=1., raw_high=1., raw_low=1., raw_close=1., volume=1000000,
        suspended=0, st_status=0))
    halted = bars.symbol.eq(SYMBOL) & bars.date.ge('2023-05-19')
    bars.loc[halted, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = float('nan')
    bars.loc[halted, ['volume', 'suspended']] = [0, 1]
    bars = execution_bars(bars)
    benchmark = pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.))
    engine = AShareExecutionBacktester()
    session = (engine.start_session(bars, benchmark, signal_date=dates[0]) if spec is None else
        ScenarioExecutionSession(engine, bars, benchmark, signal_date=dates[0], valuation=spec))
    initial = {SYMBOL: .1337}
    if initial_other_weight:
        initial['000541.SZ'] = initial_other_weight
    session.submit_targets(initial)
    session.advance()
    return session, bars, dates


@pytest.mark.parametrize('mark', [.4, .11, 0.])
def test_marks_preserve_shares_and_cannot_trade_or_qualify(mark):
    spec = replace(definitions()[0], mark=mark)
    session, bars, dates = account(spec)
    prior = session.result
    shares = prior.positions.shares.iloc[-1]
    session.submit_targets({SYMBOL: 0., '000541.SZ': .1})
    session.advance()
    result = session.result
    held = result.positions.loc[result.positions.symbol.eq(SYMBOL)]
    assert held.shares.tolist() == [shares, shares]
    assert held.mark_price.iloc[-1] == mark
    assert held.market_value.iloc[-1] == shares * mark
    assert result.rejections.reason.tolist() == ['suspended']
    assert result.trades.loc[result.trades.symbol.eq(SYMBOL)].execution_date.tolist() == [dates[1]]
    with pytest.raises(SchemaError, match='UNQUALIFIED_HELD_CLOSE'):
        qualify_valuations(result, bars, MarketEvidence((), 'manifest'), None, session_date=dates[2])
    session.submit_targets({SYMBOL: 0.})
    session.advance()
    assert session.result.positions.loc[lambda x: x.symbol.eq(SYMBOL)].shares.iloc[-1] == shares
    assert set(session.result.positions.symbol) == {SYMBOL, '000541.SZ'}
    assert all('400174' not in s for s in session.result.positions.symbol)


def test_publication_variant_is_distinct_from_hindsight():
    hindsight, bounded = definitions()[1:3]
    row = pd.Series(dict(suspended=1, raw_close=float('nan')))
    assert hindsight.hindsight and hindsight.close_mark(pd.Timestamp('2023-05-19'), SYMBOL, row) == .11
    assert not bounded.hindsight
    assert bounded.close_mark(pd.Timestamp('2023-05-19'), SYMBOL, row) == .4
    assert bounded.close_mark(pd.Timestamp('2023-05-20'), SYMBOL, row) == .4
    assert bounded.close_mark(pd.Timestamp('2023-05-22'), SYMBOL, row) == .11
    assert bounded.identity != hindsight.identity


@pytest.mark.parametrize('boundary', ['2023-06-15', '2023-06-30', '2023-08-30'])
def test_expiry_stops_before_partial_session(boundary):
    session, _, _ = account(definitions()[0], boundary=boundary)
    session.advance(); session.advance()
    before = session.result
    with pytest.raises(SchemaError, match='REASSESSMENT_REQUIRED'):
        session.advance()
    pd.testing.assert_frame_equal(session.result.nav, before.nav)
    assert session.snapshot.as_of.normalize() == pd.Timestamp('2023-05-22')


def test_ordinary_session_unchanged_and_independent():
    ordinary, bars, dates = account()
    zero, _, _ = account(definitions()[-1])
    ordinary.advance(); zero.advance()
    assert ordinary.result.positions.mark_price.iloc[-1] == 1.
    assert zero.result.positions.mark_price.iloc[-1] == 0.
    with pytest.raises(SchemaError, match='UNQUALIFIED_HELD_CLOSE'):
        qualify_valuations(ordinary.result, bars, MarketEvidence((), 'manifest'), None, session_date=dates[2])
    ordinary.advance()
    assert zero.snapshot.as_of.normalize() == dates[2]


def test_reproducible_fifo_cash_and_fees():
    first, _, _ = account(definitions()[-1])
    second, _, _ = account(definitions()[-1])
    for session in (first, second):
        session.submit_targets({SYMBOL: 0., '000541.SZ': .1})
        session.advance(); session.advance()
    for field in ('nav', 'positions', 'trades', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(first.result, field), getattr(second.result, field), check_exact=True)
    assert first.result.nav.cash.iloc[-1] == first.result.nav.cash.iloc[-2]


def test_changed_source_requires_reassessment():
    spec = definitions()[0]
    with pytest.raises(SchemaError, match='REASSESS_SOURCE_STATE'):
        spec.close_mark(pd.Timestamp('2023-05-19'), SYMBOL, pd.Series(dict(suspended=0, raw_close=.4)))


def test_failed_scenario_day_discards_other_security_fill_and_cash():
    spec = definitions()[-1]
    session, bars, dates = account(spec, initial_other_weight=.01)
    # The other security fills at the open, then fails observed-close qualification.
    bad = bars.symbol.eq('000541.SZ') & bars.date.eq(dates[2])
    bars.loc[bad, 'raw_close'] = float('nan')
    session._bars.loc[bad, 'raw_close'] = float('nan')
    prior = session.result
    evidence = MarketEvidence((), 'manifest')
    prior_marks = qualify_valuations(prior, bars, evidence, None)
    state = dict(session=session, prior=prior, prior_marks=prior_marks, bars=bars,
        dates=dates, baseline_day=dates[1], plan={dates[1]: {SYMBOL: 0., '000541.SZ': .1}},
        evidence=evidence, observations={})
    accepted, marks, blocker = consume(state, spec)
    assert blocker['date'] == '2023-05-19'
    assert 'UNQUALIFIED_HELD_CLOSE' in blocker['reason']
    assert session.result.trades.execution_date.eq(dates[2]).any()
    for field in ('nav', 'trades', 'positions', 'rejections', 'target_comparison'):
        pd.testing.assert_frame_equal(getattr(accepted, field), getattr(prior, field), check_exact=True)
    pd.testing.assert_frame_equal(marks, prior_marks)


@pytest.mark.parametrize('mark', [.4, .11, 0.])
def test_conditional_account_reconciles_with_shared_audit(mark):
    spec = replace(definitions()[0], mark=mark)
    session, bars, dates = account(spec)
    evidence = MarketEvidence((), 'manifest')
    prior = session.result
    state = dict(session=session, prior=prior,
        prior_marks=qualify_valuations(prior, bars, evidence, None), bars=bars,
        dates=dates, baseline_day=dates[1], plan={dates[1]: {SYMBOL: 0., '000541.SZ': .1}},
        evidence=evidence, observations={})
    accepted, marks, blocker = consume(state, spec)
    assert blocker['date'] == '2023-06-15'
    assert accepted.positions.loc[accepted.positions.symbol.eq(SYMBOL)].shares.eq(133700).all()
    audit = audit_ledger(accepted, bars, session.engine.config.initial_cash, valuation_prices=marks)
    assert all(audit[k] for k in ('cash', 'shares', 'equity', 'fees'))
    assert accepted.trades.loc[accepted.trades.symbol.eq(SYMBOL)].execution_date.eq(dates[1]).all()


@pytest.mark.parametrize('mark', [-1., float('nan'), float('inf')])
def test_invalid_scenario_parameters(mark):
    with pytest.raises(SchemaError, match='INVALID_MARK'):
        replace(definitions()[0], mark=mark)
