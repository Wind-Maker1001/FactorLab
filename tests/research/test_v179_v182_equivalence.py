"""Differential equivalence, causality and boundary tests for native V179 & V182.

Uses frozen original Qlib V179/V182 strategy code and frozen real historical market data
in tests/fixtures/v179_v182_original (Qlib transport classes are inert test shims only).
Reference outputs (targets.csv/fills.csv/daily.parquet) are compared strictly as oracles,
never fed as strategy inputs.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from decimal import Decimal as D
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market.calendar import SessionCalendar
from factorlab.strategies.qlib_blend_ridge import (
    ModelArtifactError,
    ModelArtifactStaleError,
    predict_residual_from_artifact,
    train_monthly_ridge_artifact,
    validate_ridge_artifact,
)
from factorlab.strategies.v179_v182 import (
    ASSETS,
    BENCHMARK_SERIES,
    V179Strategy,
    V182Strategy,
    original_plan_quantities,
    to_adjusted_holdings,
    validate_market_index,
)
from factorlab.strategies.v179_v182_native import (
    NativeV125,
    NativeV165,
    NativeV179,
    NativeV182,
    Order,
    TICKERS,
    TradeDecisionWO,
    V179Planner,
    V182Planner,
)

ROOT = Path(__file__).parents[1] / 'fixtures' / 'v179_v182_original'
ARTIFACT_DIR = Path(__file__).parents[2] / 'examples' / 'v179_v182_alpaca' / 'artifacts'


@pytest.fixture
def original_classes(monkeypatch):
    for name in ['qlib', 'qlib.backtest', 'qlib.backtest.decision', 'qlib.strategy', 'qlib.strategy.base']:
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules['qlib.backtest'].backtest = lambda *a, **kw: None
    sys.modules['qlib.backtest.decision'].Order = Order
    sys.modules['qlib.backtest.decision'].TradeDecisionWO = TradeDecisionWO
    sys.modules['qlib.strategy.base'].BaseStrategy = type('BaseStrategy', (), {})
    monkeypatch.syspath_prepend(str(ROOT))
    module_names = [
        'v48_strategy', 'v50', 'v50.strategy', 'risk_research', 'risk_research.parent',
        'v51', 'v51.strategy', 'v61', 'v61.strategy', 'v69', 'v69.strategy',
        'research_v86_v100', 'research_v86_v100.policies', 'v100', 'v100.strategy',
        'exposure_research', 'exposure_research.policies', 'v105', 'v105.strategy',
        'exposure_validation', 'exposure_validation.policies', 'v125', 'v125.strategy',
        'entry_research', 'entry_research.policies', 'entry_research.risk',
        'v150', 'v150.strategy', 'v161', 'v161.strategy',
        'reserve_research', 'reserve_research.policies', 'v165', 'v165.strategy',
        'mix_research', 'mix_research.policies',
        'v179', 'v179.strategy', 'v182', 'v182.strategy',
    ]
    saved = {n: sys.modules.pop(n) for n in module_names if n in sys.modules}
    yield {
        'v125': importlib.import_module('v125.strategy').Strategy,
        'v165': importlib.import_module('v165.strategy').Strategy,
        'v179': importlib.import_module('v179.strategy').Strategy,
        'v182': importlib.import_module('v182.strategy').Strategy,
    }
    for n in module_names:
        sys.modules.pop(n, None)
    sys.modules.update(saved)


@pytest.fixture(scope='module')
def frozen_target_market():
    mkt_dir = ROOT / 'fixtures' / 'target'
    bars = {s: pd.read_parquet(mkt_dir / f'{s}.parquet') for s in TICKERS}
    close = pd.DataFrame({s: q['close'] for s, q in bars.items()}).reindex(columns=TICKERS)
    volume = pd.DataFrame({s: q['volume'] for s, q in bars.items()}).reindex(columns=TICKERS)
    opening = pd.DataFrame({s: q['open'] for s, q in bars.items()}).reindex(columns=TICKERS)
    index = pd.read_parquet(mkt_dir / 'benchmark.parquet')['close']
    return close, volume, opening, index


@pytest.fixture(scope='module')
def frozen_artifacts():
    return {
        False: json.loads((ARTIFACT_DIR / 'v179_v182_ridge_target.json').read_text(encoding='utf-8')),
        True: json.loads((ARTIFACT_DIR / 'v179_v182_ridge_target_no_sndk.json').read_text(encoding='utf-8')),
    }


def test_all_v179_v182_fixture_hashes():
    manifest = json.loads((ROOT / 'source-manifest.json').read_text(encoding='utf-8'))
    assert len(manifest['files']) == 59
    for rel, meta in manifest['files'].items():
        raw = (ROOT / rel).read_bytes()
        assert len(raw) == meta['bytes'], rel
        assert hashlib.sha256(raw).hexdigest() == meta['sha256'], rel


@pytest.mark.parametrize('omit_sndk', [False, True])
@pytest.mark.parametrize('variant', ['v179', 'v182'])
def test_indicator_ridge_and_sleeve_differential_on_frozen_target(
    original_classes, frozen_target_market, frozen_artifacts, variant, omit_sndk
):
    close, volume, opening, index = frozen_target_market
    artifact = frozen_artifacts[omit_sndk]
    OrigCls = original_classes[variant]
    NativeCls = NativeV179 if variant == 'v179' else NativeV182
    orig = OrigCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk)
    nat = NativeCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk, ridge_artifact=artifact)

    for key in [
        'ma28', 'ma63', 'qqq_ma63', 'vol', 'breadth', 'breadth_change', 'high', 'confirmed',
        'index', 'market_fast', 'market_slow', 'index_long', 'market_dd', 'market_trend',
        'risk', 'relative10', 'score', 'drawdown', 'downside', 'down_jump', 'market_down_jump',
        'fastmom', 'semideviation', 'forecast',
    ]:
        a, b = getattr(orig, key), getattr(nat, key)
        if isinstance(a, pd.Series):
            pd.testing.assert_series_equal(a, b, rtol=1e-12, atol=1e-12)
        else:
            pd.testing.assert_frame_equal(a, b, rtol=1e-12, atol=1e-12)

    # Compare V125 growth, V165 defensive, and final blended weights across sampled weekly dates
    # spanning pre-SNDK, SNDK listing/warmup, extreme drawdown, cross-year and normal regimes.
    sample_dates = pd.DatetimeIndex([
        '2022-12-30', '2023-03-17', '2024-08-02', '2024-12-31',
        '2025-02-21', '2025-02-24', '2025-04-04', '2025-04-11',
        '2025-12-31', '2026-06-12', '2026-11-20',
    ])
    for prev in sample_dates:
        prices = close.loc[prev, TICKERS]
        valid = np.isfinite(prices) & prices.gt(0)
        for held in (
            pd.Series(0.0, index=TICKERS),
            pd.Series([12.0, 0.0, 5.0, 8.0, 4.0, 6.0, 2.0, 3.0], index=TICKERS),
        ):
            pd.testing.assert_series_equal(
                original_classes['v125'].targets(orig, prev, prices, valid, held),
                NativeV125.targets(nat, prev, prices, valid, held),
                rtol=1e-12, atol=1e-12,
            )
            pd.testing.assert_series_equal(
                original_classes['v165'].targets(orig, prev, prices, valid, held),
                NativeV165.targets(nat, prev, prices, valid, held),
                rtol=1e-12, atol=1e-12,
            )
            pd.testing.assert_series_equal(
                orig.targets(prev, prices, valid, held),
                nat.targets(prev, prices, valid, held),
                rtol=1e-12, atol=1e-12,
            )


@pytest.mark.parametrize('variant', ['v179', 'v182'])
@pytest.mark.parametrize('case', ['normal', 'no_sndk'])
def test_full_weekly_replay_matches_frozen_qlib_accounts(
    frozen_target_market, frozen_artifacts, variant, case
):
    close, volume, opening, index = frozen_target_market
    omit_sndk = case == 'no_sndk'
    artifact = frozen_artifacts[omit_sndk]
    PlannerCls = V179Planner if variant == 'v179' else V182Planner
    planner = PlannerCls(close, volume, opening, market_index=index, omit_sndk=omit_sndk, ridge_artifact=artifact)

    acct_dir = ROOT / 'accounts' / variant / 'target' / case
    orig_targets = pd.read_csv(acct_dir / 'targets.csv', parse_dates=['date', 'signal_date']).set_index('date')
    orig_fills = pd.read_csv(acct_dir / 'fills.csv', parse_dates=['date'])
    orig_daily = pd.read_parquet(acct_dir / 'daily.parquet')

    cash = 300_000.0
    fee_rate = 0.001
    holdings = {s: 0.0 for s in TICKERS}
    sim_fills = []
    sim_nav = []
    days = close.loc['2023-01-01':'2026-11-23'].index

    for day in days:
        planner.trade_calendar = SimpleNamespace(get_step_time=lambda d=day: (d, d))
        planner.trade_position = SimpleNamespace(
            get_stock_amount_dict=lambda h=holdings: {k: v for k, v in h.items() if v != 0.0},
            get_cash=lambda c=cash: c,
        )
        decision = planner.generate_trade_decision()
        # Verify sells strictly precede buys on every rebalance day.
        dirs = [o.direction for o in decision.order_list]
        assert dirs == sorted(dirs)
        for o in decision.order_list:
            # Qlib binary provider (*.day.bin) stores open prices as float32; match Qlib Exchange fill price.
            px = float(np.float32(opening.at[day, o.stock_id]))
            if o.direction == Order.SELL:
                qty = min(o.amount, holdings[o.stock_id])
                turnover = qty * px
                fee = turnover * fee_rate
                holdings[o.stock_id] -= qty
                cash += turnover - fee
                side = 'sell'
            else:
                qty = min(o.amount, max(0.0, cash / (px * (1.0 + fee_rate))))
                turnover = qty * px
                fee = turnover * fee_rate
                holdings[o.stock_id] += qty
                cash -= turnover + fee
                side = 'buy'
            if qty > 0:
                sim_fills.append(dict(
                    date=day, ticker=o.stock_id, side=side, quantity=qty,
                    requested=o.amount, price=px, turnover=turnover, fee=fee,
                ))
        eod_val = sum(holdings[s] * float(close.at[day, s]) for s in TICKERS if holdings[s] != 0.0)
        sim_nav.append(cash + eod_val)

    sim_targets = pd.DataFrame(planner.decisions).set_index('date')
    assert list(sim_targets.index) == list(orig_targets.index)
    assert list(sim_targets['signal_date']) == list(orig_targets['signal_date'])
    assert float((sim_targets[TICKERS] - orig_targets[TICKERS]).abs().max().max()) < 1e-12
    # SNDK must have zero weight before listing + 28 completed sessions (and always 0 when no_sndk).
    if omit_sndk:
        assert (sim_targets['SNDK'] == 0.0).all()
    else:
        pre_warmup = sim_targets.loc[sim_targets['signal_date'] < pd.Timestamp('2025-04-02'), 'SNDK']
        assert (pre_warmup == 0.0).all()

    sim_fills_df = pd.DataFrame(sim_fills)
    assert len(sim_fills_df) == len(orig_fills)
    assert list(sim_fills_df['ticker']) == list(orig_fills['ticker'])
    assert list(sim_fills_df['side']) == list(orig_fills['side'])
    assert float((sim_fills_df['requested'] - orig_fills['requested']).abs().max()) < 1e-10
    assert float((sim_fills_df['quantity'] - orig_fills['quantity']).abs().max()) < 1e-10
    assert float((pd.Series(sim_nav, index=days) - orig_daily['nav']).abs().max()) < 1e-2


def test_offline_online_ridge_strict_causality_and_zero_sklearn_runtime(frozen_target_market, frozen_artifacts):
    close, _, _, index = frozen_target_market
    full_artifact = frozen_artifacts[False]

    # Truncating data strictly to 2025-06-30 and training offline must match the full artifact
    # on every month <= 2025-06 (proving zero future backfill into historical decisions).
    cutoff = pd.Timestamp('2025-06-30')
    truncated_artifact = train_monthly_ridge_artifact(close.loc[:cutoff], index.loc[:cutoff], omit_sndk=False)
    for month_key, trunc_entry in truncated_artifact['models'].items():
        full_entry = full_artifact['models'][month_key]
        assert trunc_entry['train_start'] == full_entry['train_start']
        assert trunc_entry['train_cutoff'] == full_entry['train_cutoff']
        assert trunc_entry['effective_from_signal_date'] == full_entry['effective_from_signal_date']
        np.testing.assert_allclose(trunc_entry['ridge_coef'], full_entry['ridge_coef'], rtol=1e-12, atol=1e-12)
        np.testing.assert_allclose(trunc_entry['ridge_intercept'], full_entry['ridge_intercept'], rtol=1e-12, atol=1e-12)

    # Stale artifact must fail closed for a future month beyond covered_end.
    with pytest.raises(ModelArtifactStaleError):
        V179Strategy(truncated_artifact, strict_model_month=True).decide(
            dict(
                session=pd.Timestamp('2025-07-07'),
                calendar=_make_calendar(close.index),
                close=close.loc[:'2025-07-03'].iloc[-252:],
                volume=pd.DataFrame(100000.0, index=close.loc[:'2025-07-03'].iloc[-252:].index, columns=TICKERS),
                market_index=index.loc[:'2025-07-03'].iloc[-252:],
                holdings={},
                cash=100000.0,
            ),
            {},
        )

    # Tampered artifact digest or non-causal train_cutoff must be rejected.
    bad = json.loads(json.dumps(full_artifact))
    bad['models']['2025-06']['train_cutoff'] = bad['models']['2025-06']['effective_from_signal_date']
    with pytest.raises(ModelArtifactError):
        validate_ridge_artifact(bad)

    # Online inference in a clean subprocess with sklearn blocked must succeed with zero sklearn import.
    probe = (
        'import json, sys; from pathlib import Path; '
        'sys.modules["sklearn"] = None; '
        'sys.modules["sklearn.linear_model"] = None; '
        'import pandas as pd; '
        'from factorlab.strategies.v179_v182_native import NativeV179, NativeV182, TICKERS; '
        f'root = Path({str(ROOT / "fixtures" / "target")!r}); '
        f'art = json.loads(Path({str(ARTIFACT_DIR / "v179_v182_ridge_target.json")!r}).read_text(encoding="utf-8")); '
        'bars = {s: pd.read_parquet(root / f"{s}.parquet").iloc[-260:] for s in TICKERS}; '
        'close = pd.DataFrame({s: q["close"] for s, q in bars.items()}).reindex(columns=TICKERS); '
        'vol = pd.DataFrame({s: q["volume"] for s, q in bars.items()}).reindex(columns=TICKERS); '
        'idx = pd.read_parquet(root / "benchmark.parquet")["close"].reindex(close.index); '
        'w179 = NativeV179(close, vol, market_index=idx, ridge_artifact=art).targets(close.index[-1], close.iloc[-1], close.iloc[-1].gt(0), pd.Series(0., index=TICKERS)); '
        'w182 = NativeV182(close, vol, market_index=idx, ridge_artifact=art).targets(close.index[-1], close.iloc[-1], close.iloc[-1].gt(0), pd.Series(0., index=TICKERS)); '
        'assert w179.sum() <= 0.90000001 and w182.sum() <= 0.90000001; '
        'print("ZERO_SKLEARN_OK")'
    )
    src_root = str(Path(__file__).parents[2] / 'src')
    res = subprocess.run(
        [sys.executable, '-c', f'import sys; sys.path.insert(0, {src_root!r}); ' + probe],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    assert 'ZERO_SKLEARN_OK' in res.stdout


def _make_calendar(index: pd.DatetimeIndex) -> SessionCalendar:
    schedule = pd.DataFrame(
        {
            'open': pd.to_datetime(index, utc=True) + pd.Timedelta(hours=14, minutes=30),
            'close': pd.to_datetime(index, utc=True) + pd.Timedelta(hours=21),
        },
        index=index,
    )
    return SessionCalendar(schedule, 'US', 'America/New_York', 'frozen-test')


@pytest.mark.parametrize('variant_cls', [V179Strategy, V182Strategy])
def test_stateless_wrapper_holidays_crossyear_restart_splits_and_benchmark_guard(
    frozen_target_market, frozen_artifacts, variant_cls
):
    close, volume, _, index = frozen_target_market
    artifact = frozen_artifacts[False]
    cal = _make_calendar(close.index)
    strat = variant_cls(artifact)

    # 1. Holiday week-open (2025-01-21 Tuesday after MLK Day Monday 2025-01-20) triggers rebalance,
    #    while Wednesday 2025-01-22 returns no orders; restart with persisted JSON state does not duplicate.
    tue = pd.Timestamp('2025-01-21')
    prev = cal.shift(tue, -1)
    win_idx = close.loc[:prev].iloc[-252:].index
    event = dict(
        session=tue,
        calendar=cal,
        close=close.loc[win_idx],
        volume=volume.loc[win_idx],
        market_index=index.loc[win_idx],
        benchmark_series=BENCHMARK_SERIES,
        holdings={},
        cash=50_000.0,
    )
    first = strat.decide(event, {})
    assert first['weights'] is not None
    assert first['state']['last_week'] == [2025, 4]
    # Exact replay with persisted JSON checkpoint produces zero duplicate orders.
    restored = json.loads(json.dumps(first['state']))
    replay = strat.decide(event, restored)
    assert replay['weights'] is None and replay['orders'] == [] and replay['state'] == restored

    # Midweek Wednesday produces no rebalance even with empty state.
    wed = pd.Timestamp('2025-01-22')
    wed_event = dict(event, session=wed)
    assert strat.decide(wed_event, {})['weights'] is None

    # 2. Cross-year ISO week (2024-12-30 Monday is ISO week (2025, 1); 2025-01-02 Thursday is same ISO week).
    dec30 = pd.Timestamp('2024-12-30')
    prev_dec = cal.shift(dec30, -1)
    idx_dec = close.loc[:prev_dec].iloc[-252:].index
    dec_res = strat.decide(
        dict(
            session=dec30, calendar=cal, close=close.loc[idx_dec], volume=volume.loc[idx_dec],
            market_index=index.loc[idx_dec], holdings={}, cash=50_000.0,
        ),
        {},
    )
    assert dec_res['state']['last_week'] == [2025, 1]
    assert strat.decide(dict(event, session=pd.Timestamp('2025-01-02')), dec_res['state'])['orders'] == []

    # 3. Benchmark substitutes (SPY / QQQ / ^GSPC) are explicitly refused.
    for forbidden in ('SPY', 'QQQ', '^GSPC', 'SPX'):
        with pytest.raises(ValueError, match=r'\^SP500TR'):
            validate_market_index(index.loc[win_idx], prev, series_id=forbidden)

    # 4. Future data leakage in close or market_index is refused.
    with pytest.raises(ValueError, match='preceding completed session'):
        strat.decide(dict(event, close=close.loc[:tue].iloc[-252:]), {})
    with pytest.raises(ValueError, match='preceding completed session'):
        strat.decide(dict(event, market_index=index.loc[:tue].iloc[-252:]), {})

    # 5. Split / corporate-action adjustment factor conversion scales raw shares cleanly.
    factors = {s: (D('0.5') if s == 'AAPL' else D('1')) for s in ASSETS}
    raw_holdings = {'AAPL': D('10')}
    adj_holdings = to_adjusted_holdings(raw_holdings, factors)
    assert adj_holdings['AAPL'] == 20.0
    steps = dict.fromkeys(ASSETS, D('0.000000001'))
    split_dec = strat.decide(dict(event, holdings=adj_holdings, cash=50_000.0), {})
    _, deltas = original_plan_quantities(split_dec, raw_holdings=raw_holdings, factors=factors, quantity_steps=steps)
    assert set(deltas) == set(ASSETS)


@pytest.mark.parametrize('case', ['outside', 'invalid_held', 'zero_exit', 'deadband', 'cap_sell', 'cap_buy', 'missing_unheld'])
def test_planner_order_edge_cases(frozen_target_market, frozen_artifacts, case):
    close, volume, _, index = frozen_target_market
    close = close.iloc[-260:].copy()
    volume = volume.iloc[-260:].copy()
    index = index.reindex(close.index)
    day = close.index[-1]
    previous = close.index[-2]
    holdings = {s: 0.0 for s in TICKERS}
    cash = 100_000.0
    desired = pd.Series(0.0, index=TICKERS)
    if case == 'outside':
        holdings['MSFT'] = 1.0
    elif case == 'invalid_held':
        holdings['AAPL'] = 1.0
        volume.loc[previous, 'AAPL'] = np.nan
    elif case == 'missing_unheld':
        close.loc[previous, 'BA'] = np.nan
        desired['AAPL'] = 0.10
    elif case == 'zero_exit':
        holdings['AAPL'] = 0.05  # tiny position below 0.5% deadband must still fully exit when target==0
    elif case == 'deadband':
        desired['AAPL'] = 0.0049
    elif case == 'cap_buy':
        desired['AAPL'] = 0.20
        volume.loc[previous, 'AAPL'] = 10.0
    elif case == 'cap_sell':
        holdings['AAPL'] = 100.0
        volume.loc[previous, 'AAPL'] = 10.0

    planner = V179Planner(close, volume, market_index=index, ridge_artifact=frozen_artifacts[False])
    planner.targets = lambda *args: desired.copy()
    planner.trade_calendar = SimpleNamespace(get_step_time=lambda: (day, day))
    planner.trade_position = SimpleNamespace(
        get_stock_amount_dict=lambda: holdings,
        get_cash=lambda: cash,
    )
    if case in {'outside', 'invalid_held'}:
        with pytest.raises(ValueError):
            planner.generate_trade_decision()
    else:
        decision = planner.generate_trade_decision()
        if case == 'deadband':
            assert decision.order_list == []
        elif case == 'zero_exit':
            assert len(decision.order_list) == 1
            assert decision.order_list[0].direction == Order.SELL
            assert decision.order_list[0].amount == pytest.approx(0.05)
        elif case == 'cap_buy':
            assert decision.order_list[0].direction == Order.BUY
            assert decision.order_list[0].amount == pytest.approx(0.01)
        elif case == 'cap_sell':
            assert decision.order_list[0].direction == Order.SELL
            assert decision.order_list[0].amount == pytest.approx(0.01)
        elif case == 'missing_unheld':
            assert [o.stock_id for o in decision.order_list] == ['AAPL']


def test_252_session_window_matches_full_history_online_inference(frozen_target_market, frozen_artifacts):
    """Online 252-session rolling window must produce exact same targets as full 3,997-session history."""
    close, volume, _, index = frozen_target_market
    artifact = frozen_artifacts[False]
    full_179 = NativeV179(close, volume, market_index=index, ridge_artifact=artifact)
    full_182 = NativeV182(close, volume, market_index=index, ridge_artifact=artifact)
    for t in (pd.Timestamp('2025-05-09'), pd.Timestamp('2026-11-20')):
        pos = close.index.get_loc(t)
        win = close.index[pos - 251:pos + 1]
        assert len(win) == 252
        w_179 = NativeV179(close.loc[win], volume.loc[win], market_index=index.loc[win], ridge_artifact=artifact)
        w_182 = NativeV182(close.loc[win], volume.loc[win], market_index=index.loc[win], ridge_artifact=artifact)
        prices = close.loc[t]
        valid = np.isfinite(prices) & prices.gt(0)
        held = pd.Series([5.0, 0.0, 2.0, 0.0, 3.0, 1.0, 0.5, 2.0], index=TICKERS)
        pd.testing.assert_series_equal(
            full_179.targets(t, prices, valid, held),
            w_179.targets(t, prices, valid, held),
            rtol=1e-12, atol=1e-12,
        )
        pd.testing.assert_series_equal(
            full_182.targets(t, prices, valid, held),
            w_182.targets(t, prices, valid, held),
            rtol=1e-12, atol=1e-12,
        )


@pytest.mark.parametrize('variant_cls', [V179Strategy, V182Strategy])
def test_month_boundary_empty_prediction_month_crossyear_holiday_and_restart(
    frozen_target_market, frozen_artifacts, variant_cls
):
    """P2 regression: empty new-month execution row must not demand a future monthly model,
    while actual signal months missing a causal model must still fail closed.
    """
    from factorlab.strategies.qlib_blend_ridge import (
        _compute_base_series,
        predict_residual_from_artifact,
    )

    close, volume, _, index = frozen_target_market
    full_artifact = frozen_artifacts[False]
    cal = _make_calendar(close.index)

    # 1. First trading day of a new month (Monday 2025-12-01) executing on prior month's
    #    last completed trading day (Friday 2025-11-28), with artifact trained strictly
    #    through 2025-11-28 (no 2025-12 model).
    dec1 = pd.Timestamp('2025-12-01')
    nov28 = cal.shift(dec1, -1)
    assert nov28 == pd.Timestamp('2025-11-28')
    trunc_nov = train_monthly_ridge_artifact(close.loc[:nov28], index.loc[:nov28], omit_sndk=False)
    assert '2025-11' in trunc_nov['models']
    assert '2025-12' not in trunc_nov['models']

    win_nov = close.loc[:nov28].iloc[-252:].index
    month_boundary_event = dict(
        session=dec1,
        calendar=cal,
        close=close.loc[win_nov],
        volume=volume.loc[win_nov],
        market_index=index.loc[win_nov],
        benchmark_series=BENCHMARK_SERIES,
        holdings={},
        cash=50_000.0,
    )
    trunc_dec = variant_cls(trunc_nov, strict_model_month=True).decide(month_boundary_event, {})
    full_dec = variant_cls(full_artifact, strict_model_month=True).decide(month_boundary_event, {})
    assert trunc_dec['weights'] is not None
    assert trunc_dec['diagnostics']['ridge_month'] == '2025-11'
    assert trunc_dec['weights'] == pytest.approx(full_dec['weights'], rel=1e-12, abs=1e-12)
    assert trunc_dec['orders'] == full_dec['orders']

    # Direct predict_residual_from_artifact with trailing empty new-month row must skip empty 2025-12.
    extended_idx = win_nov.append(pd.DatetimeIndex([dec1]))
    ext_close = close.loc[win_nov].reindex(extended_idx)
    ext_index = index.loc[win_nov].reindex(extended_idx)
    eligible, mkt, breadth, residual, score, risk = _compute_base_series(ext_close, ext_index, omit_sndk=False)
    preds_trunc = predict_residual_from_artifact(
        ext_close[eligible], residual[eligible], score[eligible], risk[eligible],
        breadth, mkt, trunc_nov, strict_month_coverage=True,
    )
    preds_full = predict_residual_from_artifact(
        ext_close[eligible], residual[eligible], score[eligible], risk[eligible],
        breadth, mkt, full_artifact, strict_month_coverage=True,
    )
    assert preds_trunc.loc[dec1].isna().all()
    pd.testing.assert_series_equal(preds_trunc.loc[nov28], preds_full.loc[nov28], rtol=1e-12, atol=1e-12)

    # 2. Actual signal month missing a model (execution Monday 2025-12-08, signal Friday 2025-12-05)
    #    MUST still raise ModelArtifactStaleError with strict_model_month=True (both with & without diagnostics).
    dec8 = pd.Timestamp('2025-12-08')
    dec5 = cal.shift(dec8, -1)
    assert dec5 == pd.Timestamp('2025-12-05')
    win_dec5 = close.loc[:dec5].iloc[-252:].index
    missing_month_event = dict(
        session=dec8,
        calendar=cal,
        close=close.loc[win_dec5],
        volume=volume.loc[win_dec5],
        market_index=index.loc[win_dec5],
        benchmark_series=BENCHMARK_SERIES,
        holdings={},
        cash=50_000.0,
    )
    with pytest.raises(ModelArtifactStaleError):
        variant_cls(trunc_nov, strict_model_month=True).decide(missing_month_event, {}, include_diagnostics=True)
    with pytest.raises(ModelArtifactStaleError):
        variant_cls(trunc_nov, strict_model_month=True).decide(missing_month_event, {}, include_diagnostics=False)

    # Non-causal effective_from_signal_date after first valid prediction date in month must still fail.
    bad_eff = json.loads(json.dumps(trunc_nov))
    bad_eff['models']['2025-11']['effective_from_signal_date'] = '2025-11-28'
    bad_eff.pop('artifact_sha256', None)
    with pytest.raises(ModelArtifactError):
        predict_residual_from_artifact(
            ext_close[eligible], residual[eligible], score[eligible], risk[eligible],
            breadth, mkt, bad_eff, strict_month_coverage=True,
        )

    # 3. Cross-year & New Year's Day holiday-shifted first session of week:
    #    Tuesday 2024-01-02 (Monday 2024-01-01 holiday) executing on Friday 2023-12-29 signal,
    #    with artifact trained strictly through 2023-12-29 (has 2023-12, no 2024-01).
    jan2 = pd.Timestamp('2024-01-02')
    dec29 = cal.shift(jan2, -1)
    assert dec29 == pd.Timestamp('2023-12-29')
    trunc_2023 = train_monthly_ridge_artifact(close.loc[:dec29], index.loc[:dec29], omit_sndk=False)
    assert '2023-12' in trunc_2023['models']
    assert '2024-01' not in trunc_2023['models']

    win_2023 = close.loc[:dec29].iloc[-252:].index
    crossyear_event = dict(
        session=jan2,
        calendar=cal,
        close=close.loc[win_2023],
        volume=volume.loc[win_2023],
        market_index=index.loc[win_2023],
        benchmark_series=BENCHMARK_SERIES,
        holdings={},
        cash=50_000.0,
    )
    cy_trunc = variant_cls(trunc_2023, strict_model_month=True).decide(crossyear_event, {})
    cy_full = variant_cls(full_artifact, strict_model_month=True).decide(crossyear_event, {})
    assert cy_trunc['weights'] is not None
    assert cy_trunc['diagnostics']['ridge_month'] == '2023-12'
    assert cy_trunc['state']['last_week'] == [2024, 1]
    assert cy_trunc['weights'] == pytest.approx(cy_full['weights'], rel=1e-12, abs=1e-12)
    assert cy_trunc['orders'] == cy_full['orders']

    # Next trading day in same week (Wednesday 2024-01-03) produces no rebalance.
    assert variant_cls(trunc_2023, strict_model_month=True).decide(
        dict(crossyear_event, session=pd.Timestamp('2024-01-03')), cy_trunc['state']
    )['orders'] == []

    # 4. Restart consistency: fresh strategy instance with persisted JSON checkpoint produces
    #    zero duplicate orders on replay, and fresh cold replay reproduces identical decisions.
    persisted = json.loads(json.dumps(cy_trunc['state']))
    restarted_replay = variant_cls(trunc_2023, strict_model_month=True).decide(crossyear_event, persisted)
    assert restarted_replay['weights'] is None and restarted_replay['orders'] == []
    assert restarted_replay['state'] == persisted

    cold_restarted = variant_cls(trunc_2023, strict_model_month=True).decide(crossyear_event, {})
    assert cold_restarted['weights'] == cy_trunc['weights']
    assert cold_restarted['orders'] == cy_trunc['orders']
    assert cold_restarted['state'] == cy_trunc['state']


@pytest.mark.parametrize(
    'prev_month_end,first_session,month_key,next_week_exec',
    [
        ('2025-11-28', '2025-12-01', '2025-12', '2025-12-08'),  # Month starts on Monday
        ('2025-07-31', '2025-08-01', '2025-08', '2025-08-04'),  # Month starts on Friday (tight weekend window)
        ('2023-12-29', '2024-01-02', '2024-01', '2024-01-08'),  # Cross-year + New Year's Day Monday holiday
    ],
)
def test_monthly_training_timing_truncated_history_matches_full_after_first_session(
    frozen_target_market, frozen_artifacts, prev_month_end, first_session, month_key, next_week_exec
):
    """P2 regression: data ending at prior month-end cannot produce month M's model;
    adding month M's first completed trading session matures the 5-day forward label
    and reproduces the exact original full-history model for month M.
    """
    close, volume, _, index = frozen_target_market
    full_artifact = frozen_artifacts[False]
    cal = _make_calendar(close.index)

    end_prev = pd.Timestamp(prev_month_end)
    d0 = pd.Timestamp(first_session)
    exec_day = pd.Timestamp(next_week_exec)
    assert cal.shift(d0, -1) == end_prev
    assert exec_day > d0

    # 1. History ending on prior month's last completed session does NOT produce month M's model.
    art_prev = train_monthly_ridge_artifact(close.loc[:end_prev], index.loc[:end_prev], omit_sndk=False)
    assert month_key not in art_prev['models']
    assert art_prev['covered_end'] == prev_month_end

    # 2. Once month M's first trading session d0 completes close, the 5-day forward label for
    #    train_cutoff = calendar[i - 5] matures at d0 close and produces the exact month M model.
    art_d0 = train_monthly_ridge_artifact(close.loc[:d0], index.loc[:d0], omit_sndk=False)
    assert month_key in art_d0['models']
    assert art_d0['covered_end'] == first_session
    m_d0 = art_d0['models'][month_key]
    m_full = full_artifact['models'][month_key]

    assert m_d0['train_start'] == m_full['train_start']
    assert m_d0['train_cutoff'] == m_full['train_cutoff']
    assert m_d0['label_maturity_date'] == first_session
    assert m_d0['effective_from_signal_date'] == first_session
    assert pd.Timestamp(m_d0['train_cutoff']) < pd.Timestamp(m_d0['label_maturity_date']) <= pd.Timestamp(m_d0['effective_from_signal_date']) < exec_day
    assert m_d0['train_days'] == m_full['train_days']
    assert m_d0['train_rows'] == m_full['train_rows']
    np.testing.assert_allclose(m_d0['scaler_mean'], m_full['scaler_mean'], rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(m_d0['scaler_scale'], m_full['scaler_scale'], rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(m_d0['ridge_coef'], m_full['ridge_coef'], rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(m_d0['ridge_intercept'], m_full['ridge_intercept'], rtol=1e-12, atol=1e-12)

    # 3. First natural-week rebalance using a month-M signal succeeds with art_d0 and matches full_artifact.
    sig_day = cal.shift(exec_day, -1)
    assert str(sig_day.to_period('M')) == month_key
    win = close.loc[:sig_day].iloc[-252:].index
    event = dict(
        session=exec_day,
        calendar=cal,
        close=close.loc[win],
        volume=volume.loc[win],
        market_index=index.loc[win],
        benchmark_series=BENCHMARK_SERIES,
        holdings={},
        cash=50_000.0,
    )
    for cls in (V179Strategy, V182Strategy):
        with pytest.raises(ModelArtifactStaleError):
            cls(art_prev, strict_model_month=True).decide(event, {})
        dec_d0 = cls(art_d0, strict_model_month=True).decide(event, {})
        dec_full = cls(full_artifact, strict_model_month=True).decide(event, {})
        assert dec_d0['diagnostics']['ridge_month'] == month_key
        assert dec_d0['weights'] == pytest.approx(dec_full['weights'], rel=1e-12, abs=1e-12)
        assert dec_d0['orders'] == dec_full['orders']
