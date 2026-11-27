from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance_futures_trading.demo_market_collection import DemoSymbolCollections
from factorlab.integrations.binance_futures_trading.demo_market_feed import FeedPaused
from factorlab.integrations.binance_futures_trading.observability import RequestCounts


def test_funding_collection_preserves_first_availability_and_separates_forecast(tmp_path):
    from datetime import timedelta
    from decimal import Decimal
    from factorlab.trading.storage import PaperStore
    from factorlab.integrations.binance_futures_trading.demo_strategy_context import DemoStrategyContext
    from factorlab.integrations.binance_futures_trading.demo_market_feed import timestamp
    class Reader:
        def __init__(self, telemetry):
            self.telemetry = telemetry
        def share_demo_clock(self, client):
            pass
        def demo_time(self, **kwargs):
            return SimpleNamespace(lower_ms=120000, upper_ms=120001)
        def request(self, method, path, params, *, signed):
            assert method == 'GET' and not signed
            self.telemetry.before_request('feed', method, path)
            if path == '/fapi/v1/fundingRate':
                return [dict(symbol='ETHUSDT', fundingTime=60000, fundingRate='.0001', markPrice='100')]
            assert path == '/fapi/v1/premiumIndex'
            return dict(symbol='ETHUSDT', time=120000, nextFundingTime=180000,
                        lastFundingRate='.0002', markPrice='101')
    sources = DemoSymbolCollections(object(), RequestCounts(), Reader, poll_seconds=1)
    store = PaperStore(tmp_path / 'context.sqlite3')
    try:
        assert sources.funding('ETHUSDT', after=None) is None
        sources.bar_jobs['ETHUSDT', 'funding'].result(timeout=2)
        observations = sources.funding('ETHUSDT', after=None)
        context = DemoStrategyContext(store)
        for observation in observations:
            assert context.observe('ETHUSDT', **observation)
            assert not context.observe('ETHUSDT', **dict(observation,
                observed_at=observation['observed_at']+timedelta(seconds=10)))
        assert context.symbols(['ETHUSDT'], at=timestamp(90000), history_limit=10)['ETHUSDT']['history'] == {}
        history = context.symbols(['ETHUSDT'], at=timestamp(120001), history_limit=10)['ETHUSDT']['history']
        assert history['funding_settlement'][0]['payload']['funding_rate'] == Decimal('.0001')
        assert history['funding_forecast'][0]['payload']['funding_rate'] == Decimal('.0002')
        assert history['funding_settlement'][0]['observed_at'] == timestamp(120001)
        assert history['funding_forecast'][0]['effective_at'] == timestamp(180000)
    finally:
        sources.close()
        store.close()


def test_closed_bar_readers_are_independent_unsigned_and_owner_drained():
    release, blocked = Event(), Event()
    owner_thread = get_ident()
    threads = []
    class Reader:
        def __init__(self, telemetry):
            self.telemetry = telemetry
        def share_demo_clock(self, client):
            pass
        def demo_time(self, **kwargs):
            return SimpleNamespace(lower_ms=120000, upper_ms=120000)
        def request(self, method, path, params, *, signed):
            assert method == 'GET' and path == '/fapi/v1/klines' and not signed
            self.telemetry.before_request('feed', method, path)
            threads.append(get_ident())
            if params['symbol'] == 'BTCUSDT':
                blocked.set()
                assert release.wait(2)
            return [[60000, '100', '101', '99', '100', '1', 119999]]
    sources = DemoSymbolCollections(object(), RequestCounts(), Reader, poll_seconds=1)
    try:
        assert sources.bars('BTCUSDT', '1m', after=60000, history_limit=1) is None
        assert sources.bars('ETHUSDT', '1m', after=60000, history_limit=1) is None
        assert blocked.wait(2)
        sources.bar_jobs['ETHUSDT', '1m'].result(timeout=2)
        bars = sources.bars('ETHUSDT', '1m', after=60000, history_limit=1)
        assert [bar.symbol for bar in bars] == ['ETHUSDT']
        assert sources.bars('BTCUSDT', '1m', after=60000, history_limit=1) is None
        assert all(thread != owner_thread for thread in threads)
        assert not release.is_set()
    finally:
        release.set()
        sources.close()


def test_blocked_symbol_reader_does_not_block_other_symbol_or_owner(tmp_path):
    blocked, release, healthy = Event(), Event(), Event()
    owner_thread = get_ident()
    readers = []
    shared_clock = object()
    class Reader:
        timeout = 1
        monotonic = staticmethod(lambda: 1)
        def __init__(self, telemetry):
            self.threads = []
            acquired = telemetry.acquired
            def observed(receipt, previous):
                acquired(receipt, previous)
                if receipt.payload['symbol'] == 'ETHUSDT':
                    healthy.set()
            telemetry.acquired = observed
            readers.append(self)
        def share_demo_clock(self, client):
            assert client is shared_clock
        def demo_time(self, **kwargs):
            return SimpleNamespace(lower_ms=1000, upper_ms=1000)
        def request(self, method, path, params, *, signed):
            assert method == 'GET' and not signed
            self.threads.append(get_ident())
            if params['symbol'] == 'BTCUSDT':
                blocked.set()
                assert release.wait(2)
            return dict(symbol=params['symbol'], time=1000, bidPrice='100', askPrice='101')
    sources = DemoSymbolCollections(shared_clock, RequestCounts(), Reader, poll_seconds=.01)
    try:
        sources.require({'BTCUSDT', 'ETHUSDT'})
        assert blocked.wait(2)
        assert healthy.wait(2)
        with pytest.raises(FeedPaused, match='UNAVAILABLE'):
            sources.book('BTCUSDT')
        assert sources.book('ETHUSDT')['symbol'] == 'ETHUSDT'
        from dataclasses import replace
        from datetime import datetime, timezone
        from decimal import Decimal
        from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
        from factorlab.trading.storage import PaperStore, dumps
        from factorlab.integrations.binance_futures_trading.demo_instance_owner import DemoInstanceOwner
        from test_binance_demo_execution import intent
        store = PaperStore(tmp_path / 'parallel-owner.sqlite3')
        capital = PortfolioStore(tmp_path / 'parallel-capital.sqlite3')
        account_id = 'account'
        store.db.execute('CREATE TABLE demo_instance_outbox (id TEXT, payload TEXT, completed INTEGER)')
        calls = []
        filters = {'LOT_SIZE': {'stepSize': '.001'}, 'MIN_NOTIONAL': {'notional': '20'},
                   'PRICE_FILTER': {'tickSize': '.01'}}
        now = datetime.fromtimestamp(1, timezone.utc)
        session = SimpleNamespace(
            symbol_context=lambda symbol: SimpleNamespace(rules=SimpleNamespace(filters=filters)),
            _fresh_book=lambda payload, **kwargs: dict(payload, time_utc=now,
                bidPrice=Decimal(payload['bidPrice']), askPrice=Decimal(payload['askPrice'])),
            _now=lambda: now,
            adapter=SimpleNamespace(account_id=account_id, read_facts=lambda: SimpleNamespace(account_id=account_id, usdt=lambda:
                {'walletBalance': Decimal(5000), 'unrealizedProfit': Decimal(0)})))
        risk_accounts = []
        executor = PortfolioExecutor(None, capital)
        executor.enforce_portfolio_risk = risk_accounts.append
        execution = SimpleNamespace(session=session, store=store, executor=executor,
            advance=lambda decision_id, **kwargs: calls.append(decision_id) or 'WAITING')
        owner = DemoInstanceOwner(session, execution)
        owner.market_sources = sources
        try:
            for decision_id, symbol in [('blocked', 'BTCUSDT'), ('healthy', 'ETHUSDT')]:
                binding = dict(account_id=account_id, instance_id=decision_id)
                pending = dict(receipt=dict(binding=binding),
                               intents=[replace(intent(decision_id), instrument_id=symbol)])
                store.db.execute('INSERT INTO demo_instance_outbox VALUES(?,?,0)',
                    (decision_id, dumps(pending)))
            assert owner.poll() == {'healthy': 'WAITING'}
            assert calls == ['healthy']
            assert risk_accounts == ['account']
        finally:
            capital.close()
            store.close()
        assert not release.is_set()
        sources.require({'ETHUSDT'})
        assert len(readers) == 2
        assert all(thread != owner_thread for reader in readers for thread in reader.threads)
    finally:
        release.set()
        sources.close()
    assert all(not collection._thread.is_alive() for collection in sources.collections.values())
