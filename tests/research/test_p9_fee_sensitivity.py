"""P9-C01 fee boundaries, saved ledger validation and same-fill diagnostics."""
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.errors import ConfigurationError
from factorlab.dataset.identity import file_hash

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('p9_fee_sensitivity', ROOT / 'tools/research/p9_fee_sensitivity.py')
fees = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fees)


@pytest.mark.parametrize('day,side,stamp', [('2023-08-27', 'sell', 100), ('2023-08-28', 'sell', 50),
                                          ('2023-08-27', 'buy', 0), ('2023-08-28', 'buy', 0)])
def test_stamp_boundaries(day, side, stamp):
    assert fees.contracts()[fees.DATED].calculate(day, side, 100000).stamp_duty == pytest.approx(stamp)


@pytest.mark.parametrize('amount,commission,slippage', [(1000, 5, .5), (100000, 30, 50), (0, 0, 0)])
def test_commission_slippage_and_sum(amount, commission, slippage):
    cost = fees.contracts()[fees.DATED].calculate('2023-08-28', 'sell', amount)
    assert cost.commission == pytest.approx(commission)
    assert cost.slippage == pytest.approx(slippage)
    assert cost.total == pytest.approx(sum(v for k, v in cost.to_dict().items() if k != 'transaction_cost'))


@pytest.mark.parametrize('day,amount', [('2023-08-28', -1), ('not-a-date', 100)])
def test_illegal_input(day, amount):
    with pytest.raises(ConfigurationError):
        fees.contracts()[fees.DATED].calculate(day, 'sell', amount)


def test_overlap_rejected():
    with pytest.raises(ConfigurationError, match='overlap'):
        FeeSchedule([FeeRule(date(2023, 1, 1), date(2023, 8, 28)), FeeRule(date(2023, 8, 28))])


def test_default_complete_coverage_and_gap_rejected():
    schedule = fees.contracts()[fees.DATED]
    assert schedule.rules[0].effective_from == date(1900, 1, 1)
    assert schedule.rules[0].effective_to + timedelta(days=1) == schedule.rules[1].effective_from
    assert schedule.rules[-1].effective_to is None
    for day in ('1990-01-01', '2023-08-27', '2023-08-28', '2099-12-31'):
        assert sum(rule.contains(day) for rule in schedule.rules) == 1
    with pytest.raises(ConfigurationError, match='cover'):
        FeeSchedule([FeeRule(date(2023, 1, 1), date(2023, 1, 2))]).calculate('2023-01-03', 'sell', 100)


def test_legacy_unchanged():
    config = fees.load_json(fees.FROZEN)
    original = FeeSchedule([FeeRule(date(1990, 1, 1), **config['fees'])])
    for day in ('2023-08-27', '2023-08-28', '2025-01-01'):
        for side in ('buy', 'sell'):
            for amount in (0, 1000, 100000):
                assert original.calculate(day, side, amount) == fees.contracts()[fees.LEGACY].calculate(day, side, amount)


def fill(day='2023-08-28', side='sell', amount=1000):
    return dict(execution_date=pd.Timestamp(day), signal_date=pd.Timestamp(day) - pd.Timedelta(days=1),
                symbol='000001.SZ', side=side, executed_shares=100, execution_price=amount / 100,
                trade_notional=amount, **fees.contracts()[fees.LEGACY].calculate(day, side, amount).to_dict())


def test_same_fill_difference():
    detail, summary = fees.compare_fills(pd.DataFrame([fill('2023-08-27'), fill(), fill(side='buy')]),
                                       start='2023-01-01', end='2024-01-01')
    assert summary['fee_difference'] == pytest.approx(.5)
    assert summary['minimum_commission_increment'] == pytest.approx(14.1)
    assert summary['fees'][fees.LEGACY]['transaction_cost'] == pytest.approx(18.5)
    assert detail.fee_difference.tolist() == pytest.approx([0, .5, 0])


def test_pre_cut_zero_does_not_claim_post_cut_observation():
    _, summary = fees.compare_fills(pd.DataFrame([fill('2023-08-27')]), start='2023-01-01', end='2024-01-01')
    assert summary['fee_difference'] == 0
    assert summary['post_cut_fee_difference'] is None
    assert summary['post_cut_status'] == 'NOT_COMPUTABLE'


def test_no_data_not_computable():
    _, summary = fees.compare_fills(pd.DataFrame(), start='2023-01-01', end='2024-01-01')
    assert summary['status'] == 'NOT_COMPUTABLE'
    assert summary['fee_difference'] is None


@pytest.mark.parametrize('field,value', [('symbol', ''), ('execution_date', 'invalid'), ('side', 'short'),
                                        ('trade_notional', -1), ('commission', 0), ('transaction_cost', 0)])
def test_bad_fill_rejected(field, value):
    row = fill()
    row[field] = value
    with pytest.raises((ValueError, ConfigurationError)):
        fees.compare_fills(pd.DataFrame([row]), start='2023-01-01', end='2024-01-01')


def test_original_ledger_identity_drift_rejected(tmp_path):
    path = tmp_path / 'trades.parquet'
    path.write_bytes(b'original receipt')
    evidence = dict(private_artifact_sha256={'trades.parquet': file_hash(path)})
    path.write_bytes(b'changed receipt')
    with pytest.raises(ValueError, match='identity drift'):
        fees.verified_file(tmp_path, 'trades.parquet', evidence)


def test_research_identity_drift_rejected(tmp_path):
    config = fees.load_json(fees.FROZEN)
    evidence = fees.load_json(fees.EVIDENCE)
    (tmp_path / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    identity = dict(evidence['identity'], data_identity='changed')
    (tmp_path / 'inference-identity.json').write_text(json.dumps(identity), encoding='utf-8')
    with pytest.raises(ValueError, match='research identity drift'):
        fees.verify_source(tmp_path, evidence)


def test_daily_cash_and_fee_reconciliation():
    row = fill(side='buy')
    row['cash_after'] = 10000 - row['trade_notional'] - row['transaction_cost']
    nav = pd.DataFrame([dict(date=row['execution_date'], cash=row['cash_after'],
                            transaction_cost=row['transaction_cost'], buy_notional=1000, sell_notional=0)])
    fees.audit_cash(pd.DataFrame([row]), nav, 10000)
    nav.loc[0, 'transaction_cost'] += 1
    with pytest.raises(ValueError, match='daily transaction_cost'):
        fees.audit_cash(pd.DataFrame([row]), nav, 10000)
