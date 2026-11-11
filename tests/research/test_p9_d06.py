"""Delisting evidence cannot grant a mark, an A-share alias, or settlement cash."""
import pandas as pd
import pytest

from factorlab.cross_market.contracts import ContractError
from factorlab.cross_market.identity import IdentityRegistry
from factorlab.data.market_evidence_sidecar import MarketEvidence
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.oos_execution_recovery import qualify_valuations
from factorlab.strategies.shared_corporate_actions import CashDividendEvidence, saved_path_accounting
from tests.research.test_shared_corporate_actions import account


@pytest.mark.parametrize('day', ['2023-05-19', '2023-06-15', '2023-06-30', '2023-08-30'])
def test_delisting_suspension_cannot_extend_existing_carry(day):
    evidence = MarketEvidence((), 'a' * 64)
    with pytest.raises(DataQualityError, match='outside exact'):
        evidence.carry('000540.SZ', day, shares=133700, raw_close=None,
            previous_observation=dict(security='000540.SZ', date='2023-05-18',
                price='0.40', status='OBSERVED_RAW_CLOSE', volume=1, field='raw_close',
                source_sha256='b' * 64))


@pytest.mark.parametrize('engine_mark', [.40, .11, 0.])
def test_engine_mark_or_external_fund_price_is_not_account_qualification(engine_mark):
    from types import SimpleNamespace
    day = pd.Timestamp('2023-05-19')
    result = SimpleNamespace(
        positions=pd.DataFrame([dict(date=day, symbol='000540.SZ', shares=133700,
                                     mark_price=engine_mark)]),
        nav=pd.DataFrame([dict(date=day, portfolio_value=1000000.)]))
    bars = pd.DataFrame([dict(date=day, symbol='000540.SZ', raw_close=float('nan'), suspended=1)])
    with pytest.raises(SchemaError, match='UNQUALIFIED_HELD_CLOSE'):
        qualify_valuations(result, bars, MarketEvidence((), 'a' * 64), None,
                           observations={}, session_date=day)


def test_otc_code_is_not_a_normal_a_share_alias():
    aliases = pd.DataFrame([dict(instrument_id='000540-A-share-episode', symbol='000540.SZ',
        market='CN', exchange='XSHE', asset_class='equity', currency='CNY',
        security_type='common_stock', listing_date='1994-02-02', delisting_date='2023-06-29',
        valid_from='1994-02-02', valid_to='2023-06-30', available_at='2023-06-29T23:59:59+08:00',
        source_identity='issuer-2023-69', evidence='dated delisting notice')])
    identities = IdentityRegistry(aliases)
    for symbol in ('000540.SZ', '400174'):
        with pytest.raises(ContractError, match='unresolved'):
            identities.resolve(symbol, 'CN', 'XSHE', '2023-08-30', '2023-08-30T23:59:59+08:00')


@pytest.mark.parametrize('event_type', ['DELISTING_SETTLEMENT', 'OTC_REGISTRATION_PENDING'])
def test_unresolved_rights_do_not_post_cash_or_destroy_holdings(account, event_type):
    nav, trades, positions = account
    trades = trades.assign(symbol='000540.SZ')
    positions = positions.assign(symbol='000540.SZ')
    before = positions.copy(deep=True)
    event = CashDividendEvidence('000540-rights', '000540.SZ', 'issuer-2023-69',
                                 event_type=event_type)
    result = saved_path_accounting(nav, trades, positions, [event], initial_cash=1000.)
    assert result['journal'].empty
    assert result['entitlements'].empty
    assert result['unresolved'].iloc[0].state == 'EVENT_UNRESOLVED'
    assert result['unresolved'].iloc[0].reason == 'UNSUPPORTED_SHARE_CHANGE'
    assert result['ledger'].paid_cash_gross.eq(0).all()
    assert result['ledger'].cash.equals(nav.cash)
    pd.testing.assert_frame_equal(positions, before, check_exact=True)
