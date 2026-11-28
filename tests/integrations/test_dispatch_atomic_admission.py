"""Original dispatch consumption and portfolio marker share one admission cut."""
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import sqlite3
from types import SimpleNamespace

import pytest

from test_binance_demo_execution import intent
from factorlab.integrations.binance_futures_trading.demo_runtime import (
    DemoRuntime, now, reduce_demo_dispatch_event)
from factorlab.integrations.binance_futures_trading.rules import order_parameters
from factorlab.integrations.ledger_replication import BatchReceiver, EventAdmission
from factorlab.integrations.ledger_replay import AccountEventWriter, AccountReplay, DomainReplay, SnapshotPosition
from factorlab.trading.contracts import OrderSnapshot, OrderType
from factorlab.trading.ledger_reducers import reduce_portfolio_event
from factorlab.trading.state import OrderRecord
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from factorlab.trading.storage import decode, dumps


def test_cumulative_dispatch_and_attempt_resume_at_one_cut(tmp_path):
    baseline = SnapshotPosition('synthetic', 'g1', 0, None)
    binding = dict(account_id='synthetic', configuration={}, instance_id='one')
    facts = SimpleNamespace(usdt=lambda: dict(walletBalance=D(5000), unrealizedProfit=D(0)))
    plan = SimpleNamespace(account_id='synthetic', symbol='NVDAUSDT', expires_at=now()+timedelta(minutes=5))
    entry = replace(intent('entry', quantity='.05'), order_type=OrderType.LIMIT, limit_price=D(100))
    with closing(DemoRuntime(tmp_path)) as runtime, closing(PortfolioStore(
            tmp_path / 'portfolio.db')) as store, closing(sqlite3.connect(
            tmp_path / 'admission.db', isolation_level=None)) as journal, closing(BatchReceiver(
            tmp_path / 'offsite', stream='synthetic', generation='g1')) as receiver:
        runtime.start_continuous(binding, opening_equity=D(5000), stop_epoch=runtime.stop_epoch(),
                                 total_orders=2, total_notional=D(10))
        original_plan = dict(id='plan', request=dict(account='synthetic', strategy='one'), reasons={})
        store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                         ('plan', 'synthetic', 'one', 'day', dumps(original_plan)))
        order = dict(record=OrderRecord(OrderSnapshot(entry), now(), D(100)),
                     reservation_price=D(100), attempted=False, uncertain=False)
        store.db.execute('INSERT INTO portfolio_orders(id,plan,payload) VALUES (?,?,?)',
                         ('entry', 'plan', dumps(order)))
        portfolio = DomainReplay(store.db, reduce_portfolio_event, baseline=baseline)
        budget = DomainReplay(runtime.db, reduce_demo_dispatch_event, baseline=baseline)
        for name, db in [('portfolio', store.db), ('runtime', runtime.db)]:
            with closing(sqlite3.connect(tmp_path / (name + '-snapshot.db'))) as snapshot:
                db.backup(snapshot)

        def lost_ack(payload):
            assert not store.db.in_transaction and not runtime.db.in_transaction
            assert runtime.consumption(None)['posts'] == 0
            assert not store.orders('plan')[0]['attempted']
            receiver.accept(payload)
            raise TimeoutError('lost combined ACK')

        def acknowledge(payload):
            receiver.accept(payload)
            return receiver.cursor()

        admission = EventAdmission(journal, stream='synthetic', generation='g1', send=lost_ack)
        replay = AccountReplay({'portfolio': portfolio, 'demo_runtime': budget}, baseline=baseline)
        writer = AccountEventWriter(admission, replay)
        executor = PortfolioExecutor(SimpleNamespace(), store)

        def prepare(original):
            assert original == entry
            return runtime.prepare_instance_dispatch(binding, 'POST', order_parameters(original), plan,
                intent=original, facts=facts, exit_reservation=('future-exit', D(5)))

        with pytest.raises(TimeoutError):
            executor.admit_order_attempt('plan', 'entry', event_writer=writer, prepare_dispatch=prepare)
        assert runtime.consumption(None)['posts'] == 0
        assert runtime.db.execute('SELECT count(*) FROM exit_budget_reservations').fetchone()[0] == 0
        admission.send = acknowledge
        runtime.db.execute("CREATE TEMP TRIGGER fail_dispatch BEFORE INSERT ON dispatches "
                           "BEGIN SELECT RAISE(ABORT, 'injected dispatch failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected dispatch'):
            writer.resume()
        assert admission.pending() is not None
        assert runtime.consumption(None)['posts'] == 0
        runtime.db.execute('DROP TRIGGER fail_dispatch')
        assert not executor.admit_order_attempt('plan', 'entry', event_writer=writer,
                                                prepare_dispatch=prepare)
        assert runtime.consumption(None) == dict(posts=1, submitted_notional='5.00')
        assert runtime.db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('5',)]
        assert store.orders('plan')[0]['attempted']
        assert receiver.cursor().sequence == 1
    (tmp_path / 'portfolio.db').unlink()
    (tmp_path / 'runtime.sqlite3').unlink()
    with closing(sqlite3.connect(tmp_path / 'portfolio-snapshot.db', isolation_level=None)) as db, closing(
            sqlite3.connect(tmp_path / 'runtime-snapshot.db', isolation_level=None)) as budget_db, closing(
            BatchReceiver(tmp_path / 'offsite', stream='synthetic', generation='g1')) as receiver:
        replay = AccountReplay({'portfolio': DomainReplay(db, reduce_portfolio_event, baseline=baseline),
            'demo_runtime': DomainReplay(budget_db, reduce_demo_dispatch_event, baseline=baseline)}, baseline=baseline)
        replay.catch_up(receiver)
        replay.catch_up(receiver)
        assert budget_db.execute('SELECT count(*) FROM dispatches').fetchone()[0] == 1
        assert budget_db.execute('SELECT notional FROM exit_budget_reservations').fetchall() == [('5',)]
        import json
        assert decode(json.loads(db.execute('SELECT payload FROM portfolio_orders').fetchone()[0]))['attempted']


def test_alpaca_original_rate_window_and_intent_are_admitted_with_marker(tmp_path):
    from test_alpaca_execution import adapter, intent as alpaca_intent
    from factorlab.integrations.alpaca.storage import reduce_alpaca_event
    instance, official = adapter(tmp_path)
    baseline = SnapshotPosition('account-one', 'g1', 0, None)
    entry = alpaca_intent('original-alpaca')
    with closing(instance.store), closing(PortfolioStore(tmp_path / 'portfolio.db')) as store, closing(
            sqlite3.connect(tmp_path / 'admission.db', isolation_level=None)) as journal, closing(
            BatchReceiver(tmp_path / 'offsite', stream='account-one', generation='g1')) as receiver:
        plan = dict(id='plan', request=dict(account='account-one', strategy='one'), reasons={})
        store.db.execute('INSERT INTO portfolio_plans VALUES (?,?,?,?,?)',
                         ('plan', 'account-one', 'one', 'day', dumps(plan)))
        order = dict(record=OrderRecord(OrderSnapshot(entry), now(), D(100)),
                     reservation_price=D(100), attempted=False, uncertain=False)
        store.db.execute('INSERT INTO portfolio_orders(id,plan,payload) VALUES (?,?,?)',
                         (entry.client_order_id, 'plan', dumps(order)))
        replay = AccountReplay({'portfolio': DomainReplay(store.db, reduce_portfolio_event, baseline=baseline),
            'alpaca': DomainReplay(instance.store.db, reduce_alpaca_event, baseline=baseline)}, baseline=baseline)
        for name, db in [('portfolio', store.db), ('alpaca', instance.store.db)]:
            with closing(sqlite3.connect(tmp_path / (name + '-snapshot.db'))) as snapshot:
                db.backup(snapshot)

        def lost_ack(payload):
            assert not store.db.in_transaction and not instance.store.db.in_transaction
            assert instance.store.order(entry.client_order_id) is None
            assert instance.store.get('submissions') is None
            receiver.accept(payload)
            raise TimeoutError('lost Alpaca ACK')

        admission = EventAdmission(journal, stream='account-one', generation='g1', send=lost_ack)
        writer = AccountEventWriter(admission, replay)
        executor = PortfolioExecutor(SimpleNamespace(), store)
        with pytest.raises(TimeoutError):
            executor.admit_order_attempt('plan', entry.client_order_id, event_writer=writer,
                                         prepare_dispatch=instance.prepare_order_admission)
        original = admission.pending()['events'][0]['dispatch']
        def acknowledge(payload):
            receiver.accept(payload)
            return receiver.cursor()
        admission.send = acknowledge
        assert not executor.admit_order_attempt('plan', entry.client_order_id, event_writer=writer,
                                                prepare_dispatch=instance.prepare_order_admission)
        assert instance.store.order(entry.client_order_id)[0] == entry
        assert instance.store.get('submissions') == original['submissions']
        assert official.posts == 0
        with pytest.raises(PermissionError, match='execution remains disabled'):
            instance.submit_order(alpaca_intent('another-order'))
    (tmp_path / 'alpaca.sqlite').unlink()
    (tmp_path / 'portfolio.db').unlink()
    with closing(sqlite3.connect(tmp_path / 'alpaca-snapshot.db', isolation_level=None)) as db, closing(
            sqlite3.connect(tmp_path / 'portfolio-snapshot.db', isolation_level=None)) as portfolio, closing(
            BatchReceiver(tmp_path / 'offsite', stream='account-one', generation='g1')) as receiver:
        replay = AccountReplay({'portfolio': DomainReplay(portfolio, reduce_portfolio_event, baseline=baseline),
            'alpaca': DomainReplay(db, reduce_alpaca_event, baseline=baseline)}, baseline=baseline)
        replay.catch_up(receiver)
        replay.catch_up(receiver)
        import json
        assert json.loads(db.execute("SELECT payload FROM alpaca_meta WHERE key='submissions'").fetchone()[0]) == original['submissions']
        assert db.execute('SELECT client_id,reservation FROM alpaca_orders').fetchall() == [('original-alpaca', '100')]


def test_alpaca_recovered_original_identity_is_rejected_before_proposal(tmp_path):
    from test_alpaca_execution import adapter, intent as alpaca_intent, order
    instance, official = adapter(tmp_path)
    with closing(instance.store):
        official.orders['existing-original'] = order('existing-original')
        assert instance.store.order('existing-original') is None
        with pytest.raises(ValueError, match='discovered during recovery'):
            instance.prepare_order_admission(alpaca_intent('existing-original'))
        assert instance.store.order('existing-original') is not None
        assert instance.store.get('submissions') is None
        assert official.posts == 0

