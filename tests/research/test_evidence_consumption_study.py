"""Policy marks are not raw observations or execution authorization."""
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.data.evidence import json_bytes, sha256
from factorlab.data.market_evidence_sidecar import CandidateFact, MarketEvidence, POLICY
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.evidence_consumption_study import (
    OBSERVED_ONLY, execution_bars, value_positions, audit_valuation_ledger,
)
from factorlab.research.oos_execution_study import execution_bars as original_execution_bars


@pytest.fixture
def evidence_case():
    observation = dict(security='002408.SZ', date='2023-04-25', field='raw_close',
        status='OBSERVED_RAW_CLOSE', price='7.12', volume=100, source_sha256='a' * 64)
    def fact(symbol, day, field, value, kind, digest, known='EX_POST_ONLY'):
        return CandidateFact(symbol, day, field, None, value, kind, digest,
            '2023-04-29', known, 'DOCUMENT_CONFIRMED', 'RESEARCH_CANDIDATE_ONLY', 'synthetic')
    facts = (
        fact('002408.SZ', '2023-04-26', 'suspension_confirmed', True, 'ISSUER_CONFIRMED_SUSPENSION', 'b' * 64),
        fact('002408.SZ', '2023-04-26', 'valuation_mark', '7.12', 'MODEL_VALUATION_ASSUMPTION', sha256(json_bytes(observation))),
        fact('000540.SZ', '2023-05-08', 'is_st', True, 'ISSUER_RISK_DESIGNATION', 'c' * 64, 'SIGNAL_TIME_STATE_KNOWN'),
    )
    return MarketEvidence(facts, 'd' * 64), {'002408.SZ': observation}


def inputs():
    day = pd.Timestamp('2023-04-26')
    positions = pd.DataFrame(dict(date=[day] * 3, symbol=['002408.SZ', 'other', 'unknown'], shares=[100, 20, 30]))
    bars = positions[['date', 'symbol']].assign(raw_close=[np.nan, 8., np.nan])
    return positions, bars


def test_explicit_policy_and_per_holding_marks(evidence_case):
    evidence, observations = evidence_case
    positions, bars = inputs()
    original = bars.copy(deep=True)
    observed = value_positions(positions, bars, evidence, policy=OBSERVED_ONLY, previous_observations=observations)
    assert observed.valuation_status.tolist() == ['UNVALUABLE_POSITION', 'OBSERVED_MARKET_PRICE', 'UNVALUABLE_POSITION']
    carried = value_positions(positions, bars, evidence, policy=POLICY, previous_observations=observations)
    assert carried.valuation_status.tolist() == ['MODEL_VALUATION_ASSUMPTION', 'OBSERVED_MARKET_PRICE', 'UNVALUABLE_POSITION']
    assert carried.iloc[0].book_value == Decimal('712.00')
    assert carried.iloc[0].valuation_source_price_sha256 == 'a' * 64
    assert carried.iloc[0].suspension_document_sha256 == 'b' * 64
    assert pd.isna(carried.iloc[0].raw_close)
    assert carried.iloc[0].nontradable is True
    pd.testing.assert_frame_equal(bars, original)
    with pytest.raises(ValueError, match='policy'):
        value_positions(positions, bars, evidence, policy='implicit carry', previous_observations=observations)


def test_observed_resumption_wins_and_bad_evidence_fails(evidence_case):
    evidence, observations = evidence_case
    positions, bars = inputs()
    bars.loc[0, 'raw_close'] = 6.76
    marks = value_positions(positions, bars, evidence, policy=POLICY, previous_observations={})
    assert marks.iloc[0].valuation_status == 'OBSERVED_MARKET_PRICE'
    assert marks.iloc[0].valuation_price == 6.76
    bars.loc[0, 'raw_close'] = np.nan
    with pytest.raises(DataQualityError):
        value_positions(positions, bars, evidence, policy=POLICY, previous_observations={})
    positions.loc[0, 'shares'] = 0
    assert len(value_positions(positions, bars, evidence, policy=POLICY, previous_observations={})) == 2


@pytest.mark.parametrize('day,price', [('2023-04-27', np.nan), ('2023-04-26', 0.), ('2023-04-26', np.inf)])
def test_no_generic_carry_for_other_days_or_invalid_raw_prices(evidence_case, day, price):
    evidence, observations = evidence_case
    positions = pd.DataFrame([dict(date=pd.Timestamp(day), symbol='002408.SZ', shares=100)])
    bars = positions[['date', 'symbol']].assign(raw_close=price)
    marks = value_positions(positions, bars, evidence, policy=POLICY, previous_observations=observations)
    assert marks.iloc[0].valuation_status == 'UNVALUABLE_POSITION'


def test_strict_st_scope_cutoff_and_unknown_execution(evidence_case):
    evidence, _ = evidence_case
    bars = pd.DataFrame(dict(date=pd.to_datetime(['2023-05-08', '2023-05-09', '2023-05-08']),
        symbol=['000540.SZ', '000540.SZ', 'another'], suspended=[0, 0, 1],
        st_status=[3, 3, 0], raw_close=[.6, .6, 4.]))
    original = bars.copy(deep=True)
    adapted = execution_bars(bars, evidence, cutoff='2023-05-05T15:00:00+08:00')
    assert adapted.loc[0, 'is_st'] == True
    assert pd.isna(adapted.loc[0, 'trade_status'])
    assert adapted.st_status.tolist() == [3, 3, 0]
    pd.testing.assert_frame_equal(adapted.iloc[1:], original_execution_bars(bars).iloc[1:], check_dtype=False)
    pd.testing.assert_frame_equal(bars, original)
    with pytest.raises(DataQualityError):
        execution_bars(bars, evidence, cutoff='2023-04-28')


def ledger():
    day = pd.Timestamp('2023-04-26')
    trades = pd.DataFrame([dict(execution_date=day, symbol='002408.SZ', side='buy', executed_shares=100,
        execution_price=7.12, trade_notional=712., transaction_cost=1., cash_after=287.,
        commission=1., stamp_duty=0., transfer_fee=0., side_fee=0., slippage=0.)])
    positions = pd.DataFrame([dict(date=day, symbol='002408.SZ', shares=100, mark_price=7.12, market_value=712.)])
    nav = pd.DataFrame([dict(date=day, cash=287., portfolio_value=999., strategy_nav=.999, transaction_cost=1.)])
    bars = pd.DataFrame([dict(date=day, symbol='002408.SZ', raw_close=np.nan)])
    return SimpleNamespace(trades=trades, positions=positions, nav=nav), bars


def test_mixed_observed_and_decimal_assumed_equity(evidence_case):
    evidence, observations = evidence_case
    result, bars = ledger()
    observed = dict(date=pd.Timestamp('2023-04-26'), symbol='other', shares=10,
                    mark_price=8., market_value=80.)
    result.positions = pd.concat([result.positions, pd.DataFrame([observed])], ignore_index=True)
    result.trades = pd.concat([result.trades, result.trades.assign(symbol='other', executed_shares=10,
        execution_price=8., trade_notional=80., transaction_cost=0., commission=0., cash_after=207.)], ignore_index=True)
    result.nav.loc[0, 'cash'] = 207.
    bars = pd.concat([bars, pd.DataFrame([dict(date=observed['date'], symbol='other', raw_close=8.)])], ignore_index=True)
    assert audit_valuation_ledger(result, bars, 1000., evidence, policy=POLICY,
        previous_observations=observations)['status'] == 'PASSED'


def test_policy_ledger_reconciles_without_raw_patch(evidence_case):
    evidence, observations = evidence_case
    result, bars = ledger()
    audit = audit_valuation_ledger(result, bars, 1000., evidence, policy=POLICY, previous_observations=observations)
    assert audit['status'] == 'PASSED' and audit['assumed_positions'] == 1
    assert pd.isna(bars.iloc[0].raw_close)
    with pytest.raises(SchemaError, match='UNVALUABLE_POSITION'):
        audit_valuation_ledger(result, bars, 1000., evidence, policy=OBSERVED_ONLY, previous_observations=observations)


@pytest.mark.parametrize('table,column', [('trades', 'cash_after'), ('trades', 'commission'),
    ('positions', 'shares'), ('positions', 'mark_price'), ('positions', 'market_value'),
    ('nav', 'portfolio_value'), ('nav', 'strategy_nav'), ('nav', 'transaction_cost')])
def test_ledger_rejects_accounting_and_policy_drift(evidence_case, table, column):
    evidence, observations = evidence_case
    result, bars = ledger()
    getattr(result, table).loc[0, column] += 1
    with pytest.raises(SchemaError):
        audit_valuation_ledger(result, bars, 1000., evidence, policy=POLICY, previous_observations=observations)
