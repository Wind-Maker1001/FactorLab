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
from factorlab.trading.contracts import AccountSnapshot, AssetBalance, Market

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


def test_unrelated_announcements_do_not_block_original_universe():
    _, prepared = inputs()
    assert execution_factors(prepared, [dict(ex_date='2026-11-23', initiating_symbol='XYZ',
        target_symbol='XYZ')], '2026-11-23') == dict.fromkeys(ASSETS, D(1))
