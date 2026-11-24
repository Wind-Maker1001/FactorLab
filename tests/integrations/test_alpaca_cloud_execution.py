from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.integrations.alpaca.cloud_execution import (
    PreparationIncomplete, WeeklyExecution, execution_factors,
)
from factorlab.integrations.alpaca.cloud_runtime import weekly_window
from factorlab.integrations.alpaca.market_data.service import PreparedHistory
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.strategies.v69 import ASSETS
from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market, Position

NOW = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)


class Adapter:
    def __init__(self):
        self.http = SimpleNamespace(request=lambda *args, **kwargs: [])

    def recover(self):
        return self.read_account()

    def read_account(self):
        return AccountSnapshot(Market.ALPACA_US_EQUITY, 'paper',
            (AssetBalance('USD', D(100000), D(0)),), (), (), NOW, True)

    def read_account_details(self):
        return SimpleNamespace(account_id='paper', equity=D(100000), cash=D(100000))

    def read_corporate_activities(self, *, after):
        return []

    def read_asset(self, symbol):
        return {'tradable': True, 'fractionable': True}


def inputs():
    days = pd.bdate_range(end='2026-11-20', periods=252)
    all_days = days.append(pd.bdate_range('2026-11-23', '2026-12-04'))
    calendar = AlpacaSessionCalendar([
        {'date': str(day.date()), 'open': '09:30', 'close': '16:00'} for day in all_days],
        coverage_start=str(days[0].date()), coverage_end='2026-12-04')
    matrix = pd.DataFrame(100., index=days, columns=ASSETS)
    bars = pd.DataFrame([dict(instrument_id=s, session=day, raw_close=100., adjusted_close=100.)
                         for day in days for s in ASSETS])
    prepared = PreparedHistory(bars, matrix, matrix, matrix * 100, 'yahoo_ratio', pd.Timestamp(NOW))
    def quotes(instruments, **kwargs):
        return {i.instrument_id: SimpleNamespace(instrument=i, bid=D(100), ask=D(100),
            event_at=NOW, received_at=NOW, session=pd.Timestamp('2026-11-23'),
            source='ALPACA', feed='iex', valid=True, validity_reasons=(),
            decision_window=kwargs['decision_window']) for i in instruments}
    return SimpleNamespace(calendar=calendar, prepare_history=lambda *args, **kwargs: prepared,
                           refresh_quotes=quotes), prepared


def test_retained_spinoff_child_does_not_enter_original_signal_or_orders(tmp_path, monkeypatch):
    import factorlab.integrations.alpaca.cloud_execution as module
    service, _ = inputs()
    adapter = Adapter()
    account = adapter.read_account()
    from dataclasses import replace
    adapter.read_account = lambda: replace(account, positions=(
        Position('CHILD', D(2), D(10), D(10), 'USD'),))

    class FlatOriginal:
        def decide(self, event, checkpoint):
            assert 'CHILD' not in event['holdings']
            assert list(event['close'].columns) == list(ASSETS)
            return dict(weights=dict.fromkeys(ASSETS, 0.), orders=[], state={})

    monkeypatch.setattr(module, 'V69Strategy', FlatOriginal)
    worker = WeeklyExecution(tmp_path, adapter, clock=lambda: NOW)
    worker.store.db.execute('INSERT INTO portfolio_claims VALUES (?,?,?,?,?,?)',
        ('paper', 'CHILD', 'V69_CAUSAL', '2', '2', '10'))
    worker.tick(service, weekly_window(service.calendar, NOW), NOW, adapter.read_account_details())
    assert worker.execution['state'] == 'COMPLETE'
    assert worker.store.orders(worker.identity) == []
    assert 'CHILD' not in worker.store.plan(worker.identity)['request']['owned']
    worker.close()


def test_original_flat_week_commits_once_and_survives_new_process_state(tmp_path, monkeypatch):
    import factorlab.integrations.alpaca.cloud_execution as module
    service, _ = inputs()
    decisions = []
    class FlatOriginal:
        def decide(self, event, checkpoint):
            decisions.append(event)
            assert event['close'].index[-1] == pd.Timestamp('2026-11-20')
            assert event['volume'].shape == (252, 8)
            return dict(weights=dict.fromkeys(ASSETS, 0.), orders=[], state={'last_week': [2026, 48]})
    monkeypatch.setattr(module, 'V69Strategy', FlatOriginal)
    worker = WeeklyExecution(tmp_path, Adapter(), clock=lambda: NOW)
    window = weekly_window(service.calendar, NOW)
    worker.tick(service, window, NOW, worker.adapter.read_account_details())
    identity = worker.identity
    assert worker.store.plan(identity)['phase'] == 'COMPLETE'
    assert worker.store.orders(identity) == []
    worker.close()
    resumed = WeeklyExecution(tmp_path, Adapter(), clock=lambda: NOW)
    resumed.tick(service, window, NOW, resumed.adapter.read_account_details())
    assert resumed.identity == identity
    assert len(decisions) == 1
    assert resumed.execution['state'] == 'COMPLETE'
    assert resumed.store.plan(identity)['request']['signal_inputs']['checkpoint'] == {'last_week': [2026, 48]}
    resumed.close()


def test_frozen_preparation_blocks_changed_holdings_before_task_commit(tmp_path, monkeypatch):
    from dataclasses import replace
    import factorlab.integrations.alpaca.cloud_execution as module
    service, _ = inputs()
    adapter = Adapter()

    class FlatOriginal:
        def decide(self, event, checkpoint):
            return dict(weights=dict.fromkeys(ASSETS, 0.), orders=[], state={})

    monkeypatch.setattr(module, 'V69Strategy', FlatOriginal)
    create_plan = module.create_alpaca_plan

    def interrupted(*args, **kwargs):
        raise RuntimeError('interrupted')

    monkeypatch.setattr(module, 'create_alpaca_plan', interrupted)
    worker = WeeklyExecution(tmp_path, adapter, clock=lambda: NOW)
    window = weekly_window(service.calendar, NOW)
    with pytest.raises(RuntimeError, match='interrupted'):
        worker.tick(service, window, NOW, adapter.read_account_details())
    worker.close()
    monkeypatch.setattr(module, 'create_alpaca_plan', create_plan)
    account = adapter.read_account()
    adapter.read_account = lambda: replace(account, positions=(
        Position('AAPL', D(1), D(100), D(100), 'USD'),))
    resumed = WeeklyExecution(tmp_path, adapter, clock=lambda: NOW)
    resumed.tick(service, window, NOW, adapter.read_account_details())
    assert resumed.execution['state'] == 'BLOCKED_INPUTS'
    assert resumed.state['reason'] == 'FROZEN_PREPARATION_HOLDINGS_CHANGED'
    assert resumed.store.db.execute('SELECT COUNT(*) FROM portfolio_plans').fetchone()[0] == 0
    resumed.close()


def test_restart_after_preparation_before_task_commit_reuses_frozen_inputs(tmp_path, monkeypatch):
    import factorlab.integrations.alpaca.cloud_execution as module
    service, _ = inputs()
    decisions = []

    class FlatOriginal:
        def decide(self, event, checkpoint):
            decisions.append(event)
            return dict(weights=dict.fromkeys(ASSETS, 0.), orders=[],
                        state={'last_week': [2026, 48]})

    monkeypatch.setattr(module, 'V69Strategy', FlatOriginal)
    create = module.create_alpaca_plan

    def crash(*args, **kwargs):
        raise RuntimeError('process interrupted before task commit')

    monkeypatch.setattr(module, 'create_alpaca_plan', crash)
    worker = WeeklyExecution(tmp_path, Adapter(), clock=lambda: NOW)
    window = weekly_window(service.calendar, NOW)
    with pytest.raises(RuntimeError, match='before task commit'):
        worker.tick(service, window, NOW, worker.adapter.read_account_details())
    worker.close()
    monkeypatch.setattr(module, 'create_alpaca_plan', create)

    def forbidden(*args, **kwargs):
        pytest.fail('restart must not reacquire or recompute frozen preparation')

    service.prepare_history = forbidden
    resumed = WeeklyExecution(tmp_path, Adapter(), clock=lambda: NOW)
    resumed.tick(service, window, NOW, resumed.adapter.read_account_details())
    assert resumed.store.plan(resumed.identity)['phase'] == 'COMPLETE'
    assert len(decisions) == 1
    assert len(list((tmp_path / 'signals').glob('*.parquet'))) == 1
    resumed.close()


def test_nonfirst_session_never_prepares_or_submits(tmp_path):
    service, _ = inputs()
    worker = WeeklyExecution(tmp_path, Adapter(), clock=lambda: NOW)
    def forbidden(*args):
        pytest.fail('nonweekly session must not generate a signal')
    worker.prepare = forbidden
    tuesday = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)
    worker.tick(service, weekly_window(service.calendar, tuesday), tuesday,
                worker.adapter.read_account_details())
    assert worker.state['state'] == 'WAITING_NEXT_WEEK'
    assert worker.store.db.execute('SELECT COUNT(*) FROM portfolio_plans').fetchone()[0] == 0
    worker.close()


def test_overnight_action_does_not_reuse_previous_share_basis():
    _, prepared = inputs()
    with pytest.raises(PreparationIncomplete, match='EXECUTION_DAY_CORPORATE_ACTION'):
        execution_factors(prepared, [dict(ex_date='2026-11-23', initiating_symbol='AAPL',
            target_symbol='AAPL', old_rate='1', new_rate='2')], '2026-11-23')


@pytest.mark.parametrize('actions', [
    [dict(ca_type='split', ca_sub_type='forward_split', old_rate='1', new_rate='2')],
    [dict(ca_type='split', ca_sub_type='reverse_split', old_rate='10', new_rate='1')],
    [dict(ca_type='dividend', ca_sub_type='cash', cash='0.25')],
    [dict(ca_type='spinoff', ca_sub_type='spinoff', old_rate='1', new_rate='0.2')],
    [dict(ca_type='split', old_rate='1', new_rate='2'),
     dict(ca_type='dividend', cash='0.25')],
])
def test_action_basis_uses_official_execution_pair_not_previous_ratio_or_guessed_terms(actions):
    _, prepared = inputs()
    prepared.bars.loc[prepared.bars.instrument_id == 'AAPL', 'adjusted_close'] = 50.
    announcements = [dict(id=str(i), ex_date='2026-11-23', initiating_symbol='AAPL',
                          target_symbol='AAPL', **action) for i, action in enumerate(actions)]
    pairs = pd.DataFrame([dict(instrument_id='AAPL', session='2026-11-23',
        event_at=pd.Timestamp('2026-11-23T14:31Z'), raw_close='80', adjusted_close='78',
        complete=True, adjusted_bar_complete=True)])
    factors = execution_factors(prepared, announcements, '2026-11-23', execution_bars=pairs)
    assert factors['AAPL'] == D('.975')
    assert factors['QQQ'] == D(1)
    raw_quantity = D('12.3')
    assert raw_quantity / factors['AAPL'] * factors['AAPL'] == raw_quantity


def test_action_basis_rejects_prior_session_or_unclosed_pair():
    _, prepared = inputs()
    action = [dict(ex_date='2026-11-23', initiating_symbol='AAPL', target_symbol='AAPL')]
    pair = pd.DataFrame([dict(instrument_id='AAPL', session='2026-11-20',
        event_at=pd.Timestamp('2026-11-20T14:31Z'), raw_close='80', adjusted_close='78',
        complete=True, adjusted_bar_complete=True)])
    with pytest.raises(PreparationIncomplete, match='INCOMPLETE_EXECUTION_BASIS:AAPL'):
        execution_factors(prepared, action, '2026-11-23', execution_bars=pair)
    pair.loc[0, 'session'] = '2026-11-23'
    pair.loc[0, 'adjusted_bar_complete'] = False
    with pytest.raises(PreparationIncomplete, match='INCOMPLETE_EXECUTION_BASIS:AAPL'):
        execution_factors(prepared, action, '2026-11-23', execution_bars=pair)


def test_unrelated_announcements_do_not_block_original_universe():
    _, prepared = inputs()
    assert execution_factors(prepared, [dict(ex_date='2026-11-23', initiating_symbol='XYZ',
        target_symbol='XYZ')], '2026-11-23') == dict.fromkeys(ASSETS, D(1))
