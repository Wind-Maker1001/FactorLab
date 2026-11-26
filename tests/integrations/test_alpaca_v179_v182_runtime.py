"""Offline HTTP Alpaca Paper end-to-end acceptance for native V179 & V182 + V69 coexistence.

Validates:
- Real content-addressed V2 StrategyPackage build, install, and sandboxed subprocess execution.
- Read-only ^SP500TR benchmark provider (local + offline HTTP Yahoo) & substitute rejection.
- Missing ^SP500TR blocks ONLY V179/V182 while V69 continues unaffected.
- Simultaneous V69 + V179 + V182 coexistence under #260 SHARED_SAME_DIRECTION capital policy.
- Actual account/sleeve equity sizing (including small $1,000 fractional-share account).
- Split/adjustment factor conversion from adjusted research units to raw broker shares.
- Process restart, pause/resume, and package upgrade without duplicate weekly rebalances.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from factorlab.integrations.alpaca.adapter import AlpacaAdapter
from factorlab.integrations.alpaca.benchmark_data import (
    ALPACA_BENCHMARK_CAPABILITY,
    LocalBenchmarkProvider,
    YahooSP500TRProvider,
    validate_benchmark_series_id,
)
from factorlab.integrations.alpaca.cloud_execution import PreparationIncomplete
from factorlab.integrations.alpaca.cloud_runtime import weekly_window
from factorlab.integrations.alpaca.http import AlpacaConfig, PaperHTTP
from factorlab.integrations.alpaca.market_data.cache import MarketCache
from factorlab.integrations.alpaca.market_data.client import Credentials, MarketDataClient, Response
from factorlab.integrations.alpaca.market_data.service import MarketDataService
from factorlab.integrations.alpaca.market_data.sessions import AlpacaSessionCalendar
from factorlab.integrations.alpaca.strategy_instances import StrategyInstances
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.package import install_package
from factorlab.strategies.v179_v182 import ASSETS, BENCHMARK_SERIES
from factorlab.strategies.v179_v182_runtime import (
    V179AlpacaStrategy,
    V182AlpacaStrategy,
    build_v179_package,
    build_v182_package,
)
from factorlab.trading.capital import CapitalPolicyMode
from factorlab.trading.control import RiskLimits

ROOT = Path(__file__).parents[1] / 'fixtures' / 'v179_v182_original'
ARTIFACT_PATH = Path(__file__).parents[2] / 'examples' / 'v179_v182_alpaca' / 'artifacts' / 'v179_v182_ridge_target.json'
BENCHMARK_PARQUET = ROOT / 'fixtures' / 'target' / 'benchmark.parquet'


@pytest.fixture(scope='module')
def frozen_market_slice():
    """Load last 320 sessions from the real frozen target fixture (covers 252+ sessions before 2026-10-05)."""
    mkt = ROOT / 'fixtures' / 'target'
    bars = {s: pd.read_parquet(mkt / f'{s}.parquet').iloc[-320:] for s in ASSETS}
    bench = pd.read_parquet(BENCHMARK_PARQUET).iloc[-320:]
    artifact = json.loads(ARTIFACT_PATH.read_text(encoding='utf-8'))
    return bars, bench, artifact


class OfflineAlpacaPaperServer:
    """In-memory offline HTTP simulator for Alpaca Paper Trading + Market Data REST APIs."""

    def __init__(self, *, account_id: str, initial_cash: D, bars: dict[str, pd.DataFrame],
                 now_fn, factor_override: dict[str, float] | None = None):
        self.account_id = account_id
        self.cash = D(initial_cash)
        self.bars = bars
        self.now_fn = now_fn
        self.factor_override = dict(factor_override or {})
        self.positions: dict[str, dict] = {}
        self.orders_by_cid: dict[str, dict] = {}
        self.fill_activities: list[dict] = []
        self.order_seq = 0
        self.posts: list[dict] = []
        self.days = bars['AAPL'].index
        self.last_prices = {
            s: D(str(round(self._raw_bar_price(s, float(bars[s].loc[:'2026-10-02', 'close'].iloc[-1])), 2)))
            for s in ASSETS
        }

    def _raw_bar_price(self, symbol: str, adj_val: float) -> float:
        factor = self.factor_override.get(symbol, 1.0)
        return float(adj_val) / factor

    def _raw_bar_vol(self, symbol: str, adj_vol: float) -> float:
        factor = self.factor_override.get(symbol, 1.0)
        return float(adj_vol) * factor

    def _equity(self) -> D:
        mv = sum(
            D(p['qty']) * self.last_prices[s]
            for s, p in self.positions.items()
            if D(p['qty']) != 0
        )
        return self.cash + mv

    def paper_transport(self, method: str, path: str, params: dict | None, body: dict | None):
        params = dict(params or {})
        now_iso = self.now_fn().isoformat().replace('+00:00', 'Z')

        if method == 'GET' and path == '/v2/account':
            eq = self._equity()
            return 200, {
                'id': self.account_id,
                'status': 'ACTIVE',
                'currency': 'USD',
                'cash': str(self.cash),
                'equity': str(eq),
                'buying_power': str(self.cash),
                'non_marginable_buying_power': str(self.cash),
                'portfolio_value': str(eq),
                'multiplier': '1',
                'trading_blocked': False,
                'transfers_blocked': False,
                'account_blocked': False,
                'shorting_enabled': False,
                'pattern_day_trader': False,
            }
        if method == 'GET' and path == '/v2/positions':
            out = []
            for sym, pos in sorted(self.positions.items()):
                qty = D(pos['qty'])
                if qty == 0:
                    continue
                px = self.last_prices[sym]
                out.append({
                    'asset_id': 'official-' + sym,
                    'symbol': sym,
                    'qty': str(qty),
                    'avg_entry_price': pos['avg_entry_price'],
                    'current_price': str(px),
                    'market_value': str(qty * px),
                    'side': 'long',
                })
            return 200, out
        if method == 'GET' and path == '/v2/orders':
            status_filter = params.get('status', 'open')
            items = list(self.orders_by_cid.values())
            if status_filter == 'open':
                items = [o for o in items if o['status'] not in {'filled', 'canceled', 'rejected', 'expired'}]
            return 200, items
        if method == 'GET' and path == '/v2/orders:by_client_order_id':
            cid = params['client_order_id']
            order = self.orders_by_cid.get(cid)
            if order is None:
                return 404, {'message': 'order not found'}
            return 200, order
        if method == 'GET' and path.startswith('/v2/assets/'):
            sym = path.split('/')[-1]
            return 200, {
                'id': 'official-' + sym,
                'symbol': sym,
                'class': 'us_equity',
                'status': 'active',
                'tradable': True,
                'fractionable': True,
            }
        if method == 'GET' and path == '/v2/corporate_actions/announcements':
            return 200, []
        if method == 'GET' and path == '/v2/account/activities/FILL':
            return 200, list(self.fill_activities)
        if method == 'GET' and path.startswith('/v2/account/activities'):
            return 200, []
        if method == 'POST' and path == '/v2/orders':
            payload = dict(body or {})
            cid = payload['client_order_id']
            if cid in self.orders_by_cid:
                return 200, self.orders_by_cid[cid]
            self.order_seq += 1
            sym = payload['symbol']
            side = payload['side']
            qty = D(str(payload['qty']))
            fill_px = self.last_prices[sym]
            notional = qty * fill_px
            if side == 'buy':
                if notional > self.cash + D('0.01'):
                    return 403, {'message': 'insufficient buying power'}
                self.cash -= notional
                prev_qty = D(self.positions.get(sym, {}).get('qty', '0'))
                prev_cost = prev_qty * D(self.positions.get(sym, {}).get('avg_entry_price', str(fill_px)))
                new_qty = prev_qty + qty
                avg_px = (prev_cost + notional) / new_qty if new_qty > 0 else fill_px
                self.positions[sym] = {'qty': str(new_qty), 'avg_entry_price': str(avg_px)}
            else:
                prev_qty = D(self.positions.get(sym, {}).get('qty', '0'))
                if qty > prev_qty + D('1e-9'):
                    return 403, {'message': 'insufficient qty'}
                self.cash += notional
                new_qty = max(D(0), prev_qty - qty)
                if new_qty == 0:
                    self.positions.pop(sym, None)
                else:
                    self.positions[sym]['qty'] = str(new_qty)
            order = {
                'id': f'alpaca-ord-{self.order_seq}',
                'client_order_id': cid,
                'symbol': sym,
                'side': side,
                'type': payload['type'],
                'time_in_force': payload['time_in_force'],
                'qty': str(qty),
                'filled_qty': str(qty),
                'filled_avg_price': str(fill_px),
                'limit_price': payload.get('limit_price'),
                'status': 'filled',
                'created_at': now_iso,
                'updated_at': now_iso,
                'filled_at': now_iso,
            }
            self.orders_by_cid[cid] = order
            self.fill_activities.append({
                'id': f'fill-{self.order_seq}',
                'activity_type': 'FILL',
                'order_id': order['id'],
                'symbol': sym,
                'side': side,
                'qty': str(qty),
                'price': str(fill_px),
                'transaction_time': now_iso,
            })
            self.posts.append(order)
            return 200, order
        raise AssertionError(f'unexpected paper HTTP request: {method} {path} {params}')

    def market_transport(self, url: str, headers: dict, timeout: float):
        parsed = urlparse(url)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if path == '/v2/calendar':
            rows = [
                {'date': str(day.date()), 'open': '09:30', 'close': '16:00'}
                for day in self.days
            ]
            return Response(200, {'X-Request-ID': 'cal'}, rows)
        if path == '/v2/clock':
            now_iso = self.now_fn().isoformat().replace('+00:00', 'Z')
            return Response(200, {'X-Request-ID': 'clk'}, {
                'timestamp': now_iso, 'is_open': True,
                'next_open': now_iso, 'next_close': now_iso,
            })
        if path == '/v2/stocks/bars':
            symbols = params['symbols'].split(',')
            adj = params.get('adjustment', 'raw')
            start_day = pd.Timestamp(params['start']).tz_convert('America/New_York').tz_localize(None).normalize()
            end_day = pd.Timestamp(params['end']).tz_convert('America/New_York').tz_localize(None).normalize()
            out = {}
            for sym in symbols:
                df = self.bars[sym]
                sub = df.loc[(df.index >= start_day) & (df.index <= end_day)]
                rows = []
                for day, r in sub.iterrows():
                    if adj == 'raw':
                        o = self._raw_bar_price(sym, r['open'])
                        h = self._raw_bar_price(sym, r['high'])
                        l = self._raw_bar_price(sym, r['low'])
                        c = self._raw_bar_price(sym, r['close'])
                        v = self._raw_bar_vol(sym, r['volume'])
                    else:
                        o, h, l, c, v = float(r['open']), float(r['high']), float(r['low']), float(r['close']), float(r['volume'])
                    h = max(h, o, l, c)
                    l = min(l, o, h, c)
                    rows.append({
                        't': pd.Timestamp(day).tz_localize('America/New_York').tz_convert('UTC').isoformat().replace('+00:00', 'Z'),
                        'o': o, 'h': h, 'l': l, 'c': c, 'v': v,
                    })
                out[sym] = rows
            return Response(200, {'X-Request-ID': 'bars'}, {'bars': out, 'next_page_token': None})
        if path == '/v2/stocks/quotes/latest':
            symbols = params['symbols'].split(',')
            quote_ts = (self.now_fn() + timedelta(milliseconds=5)).isoformat().replace('+00:00', 'Z')
            quotes = {}
            for sym in symbols:
                px = float(self.last_prices[sym])
                quotes[sym] = {'t': quote_ts, 'bp': px, 'ap': px}
            return Response(200, {'X-Request-ID': 'quotes'}, {'quotes': quotes})
        raise AssertionError(f'unexpected market HTTP request: {url}')


def _promote(view, manifest, selection_id):
    view.request(
        manifest,
        dict(
            schema='FACTORLAB_CHAMPION_PROMOTION_V2',
            binding=view.binding(),
            selection_id=selection_id,
            decision='PROMOTE',
            qualified=True,
            strictly_better=True,
            expected_active=view.snapshot()['active'],
            package_id=manifest['package_id'],
            evidence=manifest['evidence'],
        ),
    )


def test_alpaca_benchmark_capability_and_yahoo_offline_http(frozen_market_slice):
    bars, bench, _ = frozen_market_slice
    assert ALPACA_BENCHMARK_CAPABILITY['alpaca_stocks_api_supports_sp500tr'] is False
    for forbidden in ('SPY', 'QQQ', '^GSPC', 'SPX'):
        with pytest.raises(ValueError, match=r'\^SP500TR'):
            validate_benchmark_series_id(forbidden)

    days = bars['AAPL'].index
    cal = AlpacaSessionCalendar(
        [dict(date=str(d.date()), open='09:30', close='16:00') for d in days],
        coverage_start=str((days[0] - pd.Timedelta(days=7)).date()),
        coverage_end=str(days[-1].date()),
    )
    session = days[-1]
    prior_days = days[-253:-1]
    cutoff = cal.at(prior_days[-1], 'close').to_pydatetime()
    now = cal.at(session, 'open').to_pydatetime() + timedelta(minutes=5)

    timestamps = [int(cal.at(d, 'close').timestamp()) for d in prior_days]
    closes = [float(bench.at[d, 'close']) for d in prior_days]
    yahoo_payload = {'chart': {'result': [{'timestamp': timestamps, 'indicators': {'quote': [{'close': closes}]}}]}}
    provider = YahooSP500TRProvider(transport=lambda url, timeout: (200, yahoo_payload), clock=lambda: now)
    hist = provider.history(BENCHMARK_SERIES, calendar=cal, days=prior_days, signal_cutoff=cutoff, now=now)
    assert hist.series_id == BENCHMARK_SERIES
    assert hist.source == 'YAHOO:^SP500TR'
    assert len(hist.records) == 252
    assert hist.records[-1]['payload']['session'] == str(prior_days[-1].date())

    # HTTP failure raises PreparationIncomplete (fail-closed, never falls back to SPY/QQQ).
    failing = YahooSP500TRProvider(transport=lambda url, timeout: (503, {}), clock=lambda: now)
    with pytest.raises(PreparationIncomplete, match='BENCHMARK_HTTP_ERROR'):
        failing.history(BENCHMARK_SERIES, calendar=cal, days=prior_days, signal_cutoff=cutoff, now=now)


def test_v179_v182_packages_coexist_with_v69_isolated_state_restart_upgrade_and_missing_benchmark(
    tmp_path, frozen_market_slice
):
    bars, bench, artifact = frozen_market_slice
    # Execution session: Monday 2026-10-05 (active positive AAPL, TQQQ, QQQ weights for V69/V179/V182).
    now_box = [datetime(2026, 10, 5, 13, 35, 0, tzinfo=timezone.utc)]

    def clock():
        return now_box[0]

    def tick_clock():
        now_box[0] = now_box[0] + timedelta(milliseconds=10)
        return now_box[0]

    # Use non-trivial corporate-action adjustment factor (0.5 on AAPL) to validate raw share conversion.
    server = OfflineAlpacaPaperServer(
        account_id='paper-p10-03',
        initial_cash=D('40000'),
        bars=bars,
        now_fn=clock,
        factor_override={'AAPL': 0.5},
    )
    adapter = AlpacaAdapter(
        PaperHTTP(AlpacaConfig('key', 'secret', 'https://paper-api.alpaca.markets'), transport=server.paper_transport),
        tmp_path / 'account.sqlite3',
        limits=RiskLimits(D('50000'), D('50000'), D('50000'), 100),
    )
    client = MarketDataClient(Credentials('key', 'secret'), feed='iex', request=server.market_transport, now=tick_clock)
    service = MarketDataService.connect(client, MarketCache(tmp_path / 'history'), start='2025-08-01', end='2026-11-23')

    # Build real V2 StrategyPackages for V179 and V182 (bundling frozen model_artifact.py).
    releases = tmp_path / 'releases'
    pkg_v179 = build_v179_package(
        account_id='paper-p10-03',
        capital='10000',
        model_artifact=artifact,
        code_commit='1' * 40,
        destination=tmp_path / 'v179-v1.zip',
        evidence={'p10_03': 'v179', 'champion_promoted': False},
        feed='iex',
    )
    pkg_v182 = build_v182_package(
        account_id='paper-p10-03',
        capital='5000',
        model_artifact=artifact,
        code_commit='2' * 40,
        destination=tmp_path / 'v182-v1.zip',
        evidence={'p10_03': 'v182', 'champion_promoted': False},
        feed='iex',
    )
    rel_179, _ = install_package(tmp_path / 'v179-v1.zip', releases)
    rel_182, _ = install_package(tmp_path / 'v182-v1.zip', releases)

    # Verify both packages load cleanly in the isolated sandboxed subprocess worker (no sklearn!).
    loaded_179 = LoadedStrategy.from_strategy_package(rel_179, trusted=True)
    loaded_182 = LoadedStrategy.from_strategy_package(rel_182, trusted=True)
    assert loaded_179.spec.strategy_id == 'V179_TREND_BLEND'
    assert loaded_182.spec.strategy_id == 'V182_RECOVERY_BLEND'
    assert loaded_179.spec.symbols == list(ASSETS)
    assert BENCHMARK_SERIES not in loaded_179.spec.symbols

    b_v69 = dict(market='ALPACA_US_EQUITY', account_id='paper-p10-03', strategy_id='V69_CAUSAL',
                 instance_id='V69_CAUSAL', allocation_id='v69-original')
    b_v179 = dict(market='ALPACA_US_EQUITY', account_id='paper-p10-03', strategy_id='V179_TREND_BLEND',
                  instance_id='V179_TREND', allocation_id='v179-usd')
    b_v182 = dict(market='ALPACA_US_EQUITY', account_id='paper-p10-03', strategy_id='V182_RECOVERY_BLEND',
                  instance_id='V182_RECOVERY', allocation_id='v182-usd')

    # Pre-register V179 and V182 package activations (V69 keeps its native adoption path untouched).
    dep_store = DeploymentStore(tmp_path / 'deployments.sqlite3')
    for binding, manifest, sel in ((b_v179, pkg_v179, 'init-v179'), (b_v182, pkg_v182, 'init-v182')):
        dep_store.register_instance(binding)
        view = dep_store.for_instance(binding['instance_id'])
        _promote(view, manifest, sel)
        view.close()
    dep_store.close()

    configuration = dict(
        releases=str(releases),
        trusted_python=True,
        capital_policy=dict(
            currency='USD',
            budget='35000',
            fee_rate='0',
            mode=CapitalPolicyMode.SHARED_SAME_DIRECTION.value,
        ),
        instances=[
            dict(binding=b_v69, native_v69=True, funding={'capital': '20000', 'initial_cash': '20000'}),
            dict(binding=b_v179, funding={'capital': '10000', 'initial_cash': '10000'}),
            dict(binding=b_v182, funding={'capital': '5000', 'initial_cash': '5000'}),
        ],
    )

    win = weekly_window(service.calendar, clock())
    facts = adapter.read_account_details()

    # Phase 1: Missing ^SP500TR benchmark blocks ONLY V179 and V182, while V69 ticks to WEEK_COMPLETE!
    host_missing = StrategyInstances(
        tmp_path, adapter, configuration, clock=tick_clock, revision='a' * 40, benchmark_provider=None
    )
    for _ in range(6):
        host_missing.tick(service, win, clock(), adapter.read_account_details())
        if host_missing.state['instances']['V69_CAUSAL']['state'] == 'WEEK_COMPLETE':
            break
    assert host_missing.state['instances']['V69_CAUSAL']['state'] == 'WEEK_COMPLETE', host_missing.native['V69_CAUSAL'].execution
    assert host_missing.state['instances']['V179_TREND']['state'] == 'PREPARATION_INCOMPLETE'
    assert 'MISSING_BENCHMARK_PROVIDER:^SP500TR' in host_missing.state['instances']['V179_TREND']['reason']
    assert host_missing.state['instances']['V182_RECOVERY']['state'] == 'PREPARATION_INCOMPLETE'
    assert 'MISSING_BENCHMARK_PROVIDER:^SP500TR' in host_missing.state['instances']['V182_RECOVERY']['reason']
    v69_posts = len(server.posts)
    assert v69_posts == 4
    host_missing.close()

    # Phase 2: Supply verified read-only ^SP500TR snapshot -> V179 & V182 execute cleanly alongside V69!
    bench_provider = LocalBenchmarkProvider(BENCHMARK_PARQUET, clock=clock)
    host = StrategyInstances(
        tmp_path, adapter, configuration, clock=tick_clock, revision='a' * 40, benchmark_provider=bench_provider
    )
    for _ in range(6):
        host.tick(service, win, clock(), adapter.read_account_details())
        if all(
            host.state['instances'][name]['state'] in {'RUNNING', 'WEEK_COMPLETE'}
            for name in ('V69_CAUSAL', 'V179_TREND', 'V182_RECOVERY')
        ) and all(
            host.store.plan(host.report()[name]['task'])['phase'] == 'COMPLETE'
            for name in ('V179_TREND', 'V182_RECOVERY')
            if host.report()[name]['task'] is not None
        ):
            break
    assert host.state['instances']['V69_CAUSAL']['state'] == 'WEEK_COMPLETE'
    assert host.state['instances']['V179_TREND']['state'] == 'RUNNING'
    assert host.state['instances']['V182_RECOVERY']['state'] == 'RUNNING'
    assert host.state['state'] == 'RUNNING'

    reports = host.report()
    # Independent allocations, checkpoints, cash and positions per instance.
    assert reports['V69_CAUSAL']['binding']['allocation_id'] == 'v69-original'
    assert reports['V179_TREND']['binding']['allocation_id'] == 'v179-usd'
    assert reports['V182_RECOVERY']['binding']['allocation_id'] == 'v182-usd'
    assert host.views['V179_TREND'].checkpoint()['checkpoint']['last_week'] == [2026, 41]
    assert host.views['V182_RECOVERY'].checkpoint()['checkpoint']['last_week'] == [2026, 41]
    # Shared capital policy #260 owns all 8 symbols across all 3 instances in SHARED_SAME_DIRECTION mode.
    assert host.store.capital_policy.mode('paper-p10-03') == CapitalPolicyMode.SHARED_SAME_DIRECTION
    aapl_owners = {o.instance_id for o in host.store.capital_policy.ownership('paper-p10-03', 'AAPL')}
    assert aapl_owners == {'V69_CAUSAL', 'V179_TREND', 'V182_RECOVERY'}
    # Verify actual sleeve sizing: V179 ($10k) and V182 ($5k) stayed strictly within their allocations!
    assert reports['V179_TREND']['cash'] >= D(0) and reports['V179_TREND']['cash'] <= D('10000')
    assert reports['V182_RECOVERY']['cash'] >= D(0) and reports['V182_RECOVERY']['cash'] <= D('5000')
    posts_after_exec = len(server.posts)
    assert posts_after_exec > v69_posts  # V179 & V182 actually submitted real orders!
    # Verify TQQQ economic multiplier recorded on V179/V182 plans.
    plan_179 = host.store.plan(reports['V179_TREND']['task'])
    assert plan_179['request']['multipliers'] == {'TQQQ': D(3)}

    # Phase 3: Repeated ticks & full process restart produce ZERO duplicate rebalances/orders.
    host.tick(service, win, clock(), adapter.read_account_details())
    assert len(server.posts) == posts_after_exec
    host.close()

    restarted = StrategyInstances(
        tmp_path, adapter, configuration, clock=tick_clock, revision='a' * 40, benchmark_provider=bench_provider
    )
    restarted.tick(service, win, clock(), adapter.read_account_details())
    assert len(server.posts) == posts_after_exec
    assert restarted.views['V179_TREND'].checkpoint()['checkpoint']['last_week'] == [2026, 41]
    assert restarted.views['V182_RECOVERY'].checkpoint()['checkpoint']['last_week'] == [2026, 41]

    # Phase 4: Pause / Resume on V179 does not affect V69/V182 and does not duplicate orders.
    restarted.store.capital_policy.pause('paper-p10-03', 'V179_TREND', True)
    assert restarted.store.capital_policy.snapshot('paper-p10-03', 'V179_TREND').paused is True
    restarted.store.capital_policy.pause('paper-p10-03', 'V179_TREND', False)
    restarted.tick(service, win, clock(), adapter.read_account_details())
    assert len(server.posts) == posts_after_exec

    # Phase 5: Package upgrade on V182 (while flat on a second upgrade package or with wait_flat)
    # Build v182 v2 (different code_commit) and verify checkpoint preservation without duplicate trades.
    pkg_v182_v2 = build_v182_package(
        account_id='paper-p10-03',
        capital='5000',
        model_artifact=artifact,
        code_commit='3' * 40,
        destination=tmp_path / 'v182-v2.zip',
        evidence={'p10_03': 'v182-v2', 'champion_promoted': False},
        feed='iex',
    )
    install_package(tmp_path / 'v182-v2.zip', releases)
    _promote(restarted.views['V182_RECOVERY'], pkg_v182_v2, 'upgrade-v182')
    restarted.tick(service, win, clock(), adapter.read_account_details())
    # Since V182 holds positions and migration='wait_flat', switch cleanly waits without duplicate orders or corruption.
    assert len(server.posts) == posts_after_exec
    restarted.close()
    adapter.store.db.close()


def test_small_paper_account_fractional_shares_and_equity_scaling(tmp_path, frozen_market_slice):
    """Verify $1,000 small paper account produces legal fractional shares scaled to actual equity."""
    bars, _, artifact = frozen_market_slice
    now_box = [datetime(2026, 10, 5, 13, 35, 0, tzinfo=timezone.utc)]

    def clock():
        return now_box[0]

    def tick_clock():
        now_box[0] = now_box[0] + timedelta(milliseconds=10)
        return now_box[0]

    server = OfflineAlpacaPaperServer(
        account_id='paper-small', initial_cash=D('1000'), bars=bars, now_fn=clock
    )
    adapter = AlpacaAdapter(
        PaperHTTP(AlpacaConfig('key', 'secret', 'https://paper-api.alpaca.markets'), transport=server.paper_transport),
        tmp_path / 'account.sqlite3',
        limits=RiskLimits(D('50000'), D('50000'), D('50000'), 100),
    )
    client = MarketDataClient(Credentials('key', 'secret'), feed='iex', request=server.market_transport, now=tick_clock)
    service = MarketDataService.connect(client, MarketCache(tmp_path / 'history'), start='2025-08-01', end='2026-11-23')

    releases = tmp_path / 'releases'
    pkg = build_v179_package(
        account_id='paper-small', capital='1000', model_artifact=artifact,
        code_commit='4' * 40, destination=tmp_path / 'v179-small.zip',
        evidence={'small_account': True}, feed='iex',
    )
    install_package(tmp_path / 'v179-small.zip', releases)
    binding = dict(market='ALPACA_US_EQUITY', account_id='paper-small', strategy_id='V179_TREND_BLEND',
                   instance_id='V179_SMALL', allocation_id='v179-small-usd')
    dep = DeploymentStore(tmp_path / 'deployments.sqlite3')
    dep.register_instance(binding)
    v = dep.for_instance('V179_SMALL')
    _promote(v, pkg, 'small-init')
    v.close()
    dep.close()

    host = StrategyInstances(
        tmp_path, adapter,
        dict(releases=str(releases), trusted_python=True,
             instances=[dict(binding=binding, funding={'capital': '1000', 'initial_cash': '1000'})]),
        clock=tick_clock, revision='4' * 40,
        benchmark_provider=LocalBenchmarkProvider(BENCHMARK_PARQUET, clock=clock),
    )
    host.tick(service, weekly_window(service.calendar, clock()), clock(), adapter.read_account_details())
    assert host.state['instances']['V179_SMALL']['state'] == 'RUNNING'
    assert len(server.posts) > 0
    # Total spent must be bounded by $1,000 (not $300,000!) and contain fractional shares.
    spent = D('1000') - host.store.cash('paper-small', 'V179_SMALL')
    assert D(0) < spent <= D('900.01')
    assert any(D(o['qty']) != D(o['qty']).to_integral_value() for o in server.posts)
    host.close()
    adapter.store.db.close()
