from dataclasses import replace

import pandas as pd
import pytest

from factorlab.errors import SchemaError
from factorlab.strategies.shared_corporate_actions import CashDividendEvidence, saved_path_accounting


@pytest.fixture
def account():
    dates = pd.to_datetime(['2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08', '2024-01-09'])
    trades = pd.DataFrame(dict(execution_date=dates[:3], side=['buy', 'buy', 'sell'],
        symbol=['000001.SZ'] * 3, executed_shares=[10, 5, 5], execution_price=[10., 10., 8.],
        trade_notional=[100., 50., 40.], transaction_cost=[1., 1., 1.],
        cash_after=[899., 848., 887.], commission=[1., 1., 1.], stamp_duty=[0.] * 3,
        transfer_fee=[0.] * 3, side_fee=[0.] * 3, slippage=[0.] * 3))
    positions = pd.DataFrame(dict(date=dates, symbol=['000001.SZ'] * 5,
        shares=[10, 15, 10, 10, 10], market_value=[100., 150., 80., 80., 80.]))
    nav = pd.DataFrame(dict(date=dates, cash=[899., 848., 887., 887., 887.],
        portfolio_value=[999., 998., 967., 967., 967.],
        strategy_nav=[.999, .998, .967, .967, .967], transaction_cost=[1., 1., 1., 0., 0.]))
    return nav, trades, positions


@pytest.fixture
def event():
    return CashDividendEvidence('e1', '000001.SZ', 'issuer:notice-1',
        announcement_date='2024-01-02', public_at='2024-01-02T17:00:00+08:00',
        record_date='2024-01-04', ex_date='2024-01-05', cash_per_share='2',
        implementation_evidence_id='issuer:implementation-1', payment_date='2024-01-06',
        payment_evidence_id='account:settlement-1')


def run(account, events, **kwargs):
    return saved_path_accounting(*account, events, initial_cash=1000., **kwargs)


def test_record_close_quantity_ex_date_sale_and_non_session_payment(account, event):
    before = [frame.copy(deep=True) for frame in account]
    result = run(account, [event])
    ledger = result['ledger']
    assert result['entitlements'].iloc[0].eligible_shares == 15
    assert ledger.receivable_gross.tolist() == [0, 0, 30, 0, 0]
    assert ledger.paid_cash_gross.tolist() == [0, 0, 0, 30, 30]
    assert ledger.bounded_equity_gross.tolist() == [999, 998, 997, 997, 997]
    assert ledger.dividend_income_gross.tolist() == [0, 0, 30, 30, 30]
    assert ledger.dividend_tax.iloc[2:].isna().all()
    assert ledger.investor_total_return.isna().all()
    assert result['total_return'] is None
    payment = result['journal'].query("state == 'DIVIDEND_CASH_PAID'").iloc[0]
    assert payment.date == pd.Timestamp('2024-01-06')
    assert payment.gross_cash_delta == 30
    assert payment.receivable_delta == -30
    for actual, original in zip(account, before):
        pd.testing.assert_frame_equal(actual, original)


def test_unverified_payment_stays_receivable(account, event):
    result = run(account, [replace(event, payment_evidence_id=None)])
    assert result['ledger'].receivable_gross.tolist() == [0, 0, 30, 30, 30]
    assert result['ledger'].paid_cash_gross.eq(0).all()
    assert result['unresolved'].iloc[0].reason == 'ACTUAL_PAYMENT_UNVERIFIED'


@pytest.mark.parametrize('changes', [dict(implementation_evidence_id=None), dict(public_at=None),
    dict(record_date=None), dict(cash_per_share=None), dict(event_type='STOCK_DIVIDEND'),
    dict(event_type='CAPITALIZATION'), dict(event_type='RIGHTS_ISSUE'), dict(event_type='MERGER'),
    dict(event_type='DELISTING_SETTLEMENT')])
def test_incomplete_and_unsupported_remain_unknown(account, event, changes):
    result = run(account, [replace(event, **changes)])
    assert result['entitlements'].empty
    assert result['journal'].empty
    assert len(result['unresolved']) == 1
    assert result['total_return'] is None


def test_no_zero_entitlement_when_record_observation_missing(account, event):
    result = run(account, [replace(event, record_date='2024-01-02')])
    assert result['entitlements'].empty
    assert result['unresolved'].iloc[0].reason == 'LIFECYCLE_OUTSIDE_SAVED_SESSIONS'


def test_no_entitlement_for_unheld_security(account, event):
    result = run(account, [replace(event, symbol='000002.SZ')])
    assert result['entitlements'].iloc[0].gross_entitlement == 0
    assert result['ledger'].bounded_equity_gross.tolist() == account[0].portfolio_value.tolist()


def test_evidenced_tax_is_separate_from_unchanged_trading_fees(account, event):
    result = run(account, [replace(event, tax_rate='0.1', tax_evidence_id='account:lot-tax')])
    assert result['ledger'].dividend_tax.tolist() == [0, 0, 0, 3, 3]
    assert result['ledger'].settlement_cash_net.iloc[-1] == 914
    assert result['ledger'].transaction_cost.sum() == 3


@pytest.mark.parametrize('changes', [dict(ex_date='2024-01-04'), dict(payment_date='2024-01-04'),
    dict(cash_per_share='NaN'), dict(cash_per_share='-1'), dict(tax_rate='0.1'),
    dict(tax_rate='1.1', tax_evidence_id='tax'), dict(public_at='2024-01-05T09:00:00')])
def test_invalid_evidence_is_rejected(account, event, changes):
    with pytest.raises(SchemaError):
        run(account, [replace(event, **changes)])


def test_no_double_posting_or_adjusted_price_overlay(account, event):
    with pytest.raises(SchemaError, match='duplicate'):
        run(account, [event, event])
    with pytest.raises(SchemaError, match='duplicate'):
        run(account, [event, replace(event, event_id='different-source-row')])
    with pytest.raises(SchemaError, match='double-count'):
        run(account, [event], price_basis='HFQ')


def test_candidate_intersection_uses_record_close_not_current_holding(tmp_path):
    import importlib.util
    from io import BytesIO
    from pathlib import Path
    import sys
    import zipfile

    tool = Path(__file__).resolve().parents[2] / 'tools/research/p9_corporate_action_accounting.py'
    sys.path.insert(0, str(tool.parent))
    spec = importlib.util.spec_from_file_location('p9_c02_tool', tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stock = tmp_path / 'stock.zip'
    # A sold-after-record position qualifies; an ex-date-only purchase does not.
    events = pd.DataFrame(dict(instrument=['000001.SZ', '000002.SZ', '000003.SZ'],
        date=pd.to_datetime(['2024-01-04'] * 3), publish_date=pd.to_datetime(['2024-01-02'] * 3),
        register_date=pd.to_datetime(['2024-01-04'] * 3), ex_date=pd.to_datetime(['2024-01-05'] * 3),
        cash_before_tax=[1., 1., 1.], bonus_rate=[0., 0., 0.], conversed_rate=[0., 0., 0.]))
    events['instrument'] = events.instrument.astype('string')
    with zipfile.ZipFile(stock, 'w') as archive:
        for table in ('cn_stock_dividend', 'cn_stock_allotment'):
            frame = events if table == 'cn_stock_dividend' else events.iloc[:0].rename(columns={'ex_date': 'exright_date'})
            stream = BytesIO()
            frame.to_parquet(stream)
            archive.writestr(f'stock/{table}/2024-01-01_2024-12-31.parquet', stream.getvalue())
    holdings = pd.DataFrame(dict(date=pd.to_datetime(['2024-01-04', '2024-01-05']),
        symbol=['000001.SZ', '000002.SZ']))
    candidates, native, shards = module.discover(stock, {'a': holdings}, '2024-01-03', '2024-01-10',
        pd.to_datetime(['2024-01-03', '2024-01-04', '2024-01-05', '2024-01-08']))
    assert [c['symbol'] for c in candidates] == ['000001.SZ']
    assert candidates[0]['affected_accounts'] == 'a'
    assert len(native) == 1
    assert len(shards) == 2


def test_trade_cash_corruption_cannot_be_hidden_by_dividend(account, event):
    account[0].loc[2, 'cash'] += 30
    with pytest.raises(SchemaError, match='cash ledger'):
        run(account, [event])
