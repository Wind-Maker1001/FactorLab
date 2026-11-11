"""P9-02 uses the existing executor, audit and field-scoped sidecar."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from factorlab.backtest.execution import AShareExecutionBacktester
from factorlab.errors import DataQualityError, SchemaError
from factorlab.research.oos_execution_recovery import recovery_bars, qualify_valuations, audit_frozen_fees
from factorlab.strategies.real_shared_portfolio import audit_ledger

spec = importlib.util.spec_from_file_location('p9_evidence_fixtures', Path(__file__).with_name('test_p9_market_evidence.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
case = fixtures.case


def suspended_account():
    dates = pd.to_datetime(['2023-04-24', '2023-04-25', '2023-04-26', '2023-04-27', '2023-04-28', '2023-05-04', '2023-05-05'])
    prices = [7.12, 7.12, None, None, None, None, 6.76]
    bars = pd.DataFrame(dict(date=dates, symbol='002408.SZ', raw_open=prices, raw_close=prices,
        volume=[100000, 100000, 0, 0, 0, 0, 100000], suspended=[0, 0, 1, 1, 1, 1, 0],
        trade_status=[1, 1, 0, 0, 0, 0, 1], is_st=0))
    targets = pd.DataFrame(dict(date=dates[:2], symbol='002408.SZ', target_weight=[.1, 0.]))
    result = AShareExecutionBacktester().run_target_weights(targets, bars,
        pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.)))
    return bars, result


def test_carry_qualification_and_full_ledger(case):
    _, observation, load, _, _ = case
    bars, result = suspended_account()
    qualified = qualify_valuations(result, bars, load(), observation)
    assumed = qualified.loc[qualified.valuation_assumption_used]
    assert len(assumed) == 4
    assert assumed.observed_raw_close.isna().all()
    assert assumed.valuation_price.eq(7.12).all()
    assert assumed.valuation_source_date.eq('2023-04-25').all()
    assert assumed.valuation_status.eq('MODEL_VALUATION_ASSUMPTION').all()
    assert qualified.iloc[-1].valuation_price == 6.76
    assert not qualified.iloc[-1].valuation_assumption_used
    assert bars.loc[bars.suspended.eq(1), 'raw_close'].isna().all()
    assert audit_ledger(result, bars, 1000000, valuation_prices=qualified)['qualified_marks']
    with pytest.raises(SchemaError):
        audit_ledger(result, bars, 1000000)


def test_missing_sidecar_and_wrong_mark_block(case):
    payload, observation, load, _, _ = case
    valid = load()
    bars, result = suspended_account()
    payload['candidates'] = [r for r in payload['candidates'] if r['field'] != 'suspension_confirmed']
    with pytest.raises(SchemaError, match='2023-04-26/002408.SZ'):
        qualify_valuations(result, bars, load(), observation)
    bars, result = suspended_account()
    result.positions.loc[result.positions.date.eq('2023-04-26'), 'mark_price'] = 7.14
    with pytest.raises(SchemaError, match='ENGINE_MARK_DIFFERS'):
        qualify_valuations(result, bars, valid, observation)


def test_targeted_st_and_limit_rejection(case):
    evidence = case[2]()
    dates = pd.to_datetime(['2023-05-04', '2023-05-05', '2023-05-08', '2023-05-09', '2023-05-10'])
    bars = pd.DataFrame(dict(date=dates, symbol='000540.SZ', raw_open=[.63, .63, .60, .61, .62],
        raw_close=[.63, .63, .60, .61, .62], volume=100000, suspended=0, st_status=[0, 1, 2, 1, 1]))
    untouched = bars.copy(deep=True)
    data = recovery_bars(bars, evidence, cutoff='2023-05-05T15:00:00+08:00')
    pd.testing.assert_frame_equal(bars, untouched)
    assert data.loc[data.date.eq('2023-05-08'), 'st_status'].item() == 2
    assert data.loc[data.date.eq('2023-05-08'), 'is_st'].item() == 1
    with pytest.raises(DataQualityError):
        recovery_bars(bars, evidence, cutoff='2023-04-28T15:00:00+08:00')
    target = pd.DataFrame(dict(date=dates[[0, 1, 3]], symbol='000540.SZ', target_weight=[.1, 0., 0.]))
    result = AShareExecutionBacktester().run_target_weights(target, data,
        pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.)))
    rejection = result.rejections.loc[result.rejections.execution_date.eq('2023-05-08')]
    assert rejection.reason.eq('sell_price_limit').all() and len(rejection) == 1
    assert not result.trades.execution_date.eq('2023-05-08').any()
    nav = result.nav.set_index('date')
    assert nav.loc['2023-05-08', 'cash'] == nav.loc['2023-05-05', 'cash']
    assert result.positions.date.eq('2023-05-08').any()
    assert not result.trades.execution_date.eq('2023-05-09').any()
    assert result.trades.loc[result.trades.side.eq('sell'), 'execution_date'].item() == dates[-1]
    assert audit_ledger(result, data, 1000000)['status'] == 'PASSED'


def test_new_suspension_gap_remains_blocked(case):
    dates = pd.to_datetime(['2023-04-25', '2023-04-26', '2023-04-28', '2023-05-04', '2023-05-05', '2023-05-08'])
    prices = [.63, .63, .63, None, .63, .60]
    bars = pd.DataFrame(dict(date=dates, symbol='000540.SZ', raw_open=prices, raw_close=prices,
        volume=[100000, 100000, 100000, 0, 100000, 100000], suspended=[0,0,0,1,0,0],
        trade_status=[1,1,1,0,1,1], is_st=1))
    targets = pd.DataFrame(dict(date=dates[[0,4]], symbol='000540.SZ', target_weight=[.1,0.]))
    result = AShareExecutionBacktester().run_target_weights(targets, bars,
        pd.DataFrame(dict(date=dates, adj_open=1., adj_close=1.)))
    assert result.rejections.reason.eq('sell_price_limit').any()
    with pytest.raises(SchemaError, match='2023-05-04/000540.SZ'):
        qualify_valuations(result, bars, case[2](), case[1])
    assert pd.isna(bars.loc[bars.date.eq('2023-05-04'), 'raw_close'].item())


def test_frozen_fee_receipts_recalculated(case):
    _, result = suspended_account()
    config = dict(fees=dict(commission_bps=3, minimum_commission_cny=5,
        slippage_bps=5, stamp_duty_bps=10, transfer_fee_bps=0))
    audit_frozen_fees(result, config)
    result.trades.loc[0, 'commission'] += 1
    with pytest.raises(SchemaError, match='FROZEN_FEE_RECEIPT_MISMATCH'):
        audit_frozen_fees(result, config)


def test_stage_a_block_prevents_all_research_accounts(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    runner_path = Path(__file__).resolve().parents[2] / 'tools/research/run_oos_recovery.py'
    runner_spec = importlib.util.spec_from_file_location('p9_recovery_runner', runner_path)
    runner = importlib.util.module_from_spec(runner_spec)
    runner_spec.loader.exec_module(runner)
    original = tmp_path / 'original'
    original.mkdir()
    (original / 'config.json').write_text('{}')
    output = tmp_path / 'output'
    monkeypatch.setattr(runner, 'load_inputs', lambda *_: (None, None, None, None, None))
    monkeypatch.setattr(runner, 'verify_saved', lambda *_: (None, None, {}, []))
    monkeypatch.setattr(runner, 'evidence_inputs', lambda *_: (None, None))
    monkeypatch.setattr(runner, 'recovery_bars', lambda *_, **__: None)
    monkeypatch.setattr(runner, 'stage_a', lambda *_: dict(account='stage-a/000540.SZ',
        reason='UNQUALIFIED_HELD_CLOSE: 2023-05-04/000540.SZ'))
    def forbidden(*_, **__):
        pytest.fail('Stage B must not run after Stage A blocks')
    monkeypatch.setattr(runner, 'execute', forbidden)
    runner.run(SimpleNamespace(original=original, output=output))
    result = json.loads((output / 'result.json').read_bytes())
    assert result['status'] == 'P9_02_BLOCKED'
    assert result['completed_accounts'] == 0
    assert result['stage'] == 'A'
    assert json.loads((output / 'fold-metrics.json').read_bytes()) == []


def test_no_global_enum_mapping(case):
    bars = pd.DataFrame(dict(date=pd.to_datetime(['2023-05-08', '2023-05-09']),
        symbol=['000540.SZ', '000541.SZ'], st_status=2, suspended=0))
    data = recovery_bars(bars, case[2](), cutoff='2023-05-05T15:00:00+08:00')
    assert pd.isna(data.iloc[1].is_st)
