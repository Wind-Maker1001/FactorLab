"""P8-PERF-01 correctness, financial semantics, and isolation regression suite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import numpy as np
import pytest

from factorlab.compute.parallel import (
    chunksize_for_tasks,
    create_process_pool,
    recommended_workers,
    single_thread_blas_initializer,
)
from factorlab.experiments.grid_f11_f08_offline import (
    DEFAULT_DEMO_WALLET,
    DEFAULT_STRATEGY_CAP,
    DemoGuardrails,
    FundingSchedule,
    default_demo_rules,
    execution_profile,
    run_offline_lifecycle,
)
from factorlab.experiments.grid_funding import position_settlements
from factorlab.experiments.grid_funding_adaptive import (
    DEMO_OBSERVED_HOURLY_FUNDING_BPS,
    FundingPolicy,
    VolatilityPolicy,
    causal_funding_rate_bps_h,
    run_paired_window,
    run_paired_window_multi_overlay,
)
from factorlab.experiments.grid_monte_carlo import close_bars
from factorlab.research.monte_carlo import _loss_tail
from factorlab.research.monte_carlo.reliability import tail_statistics
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridExecution, GridRules
from factorlab.strategies.loader import LoadedStrategy, StrategyWorkerError
from factorlab.trading.contracts import FundingEvent, Market, OrderIntent, OrderType, Side, funding_event_id
from factorlab.trading.control import RiskLimits, TradingController
from factorlab.trading.paper import Instrument, PaperBroker, Quote
from factorlab.trading.storage import PaperStore, decode, dumps
from factorlab.trading.strategy import ClosedBar

D = Decimal


def _worker_read_blas_env(_: int) -> dict[str, str | None]:
    import os

    return {
        "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
        "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
    }


def _sample_hourly_bars(n: int = 72, anchor: float = 132.89) -> list[dict]:
    rng = np.random.default_rng(12345)
    rets = rng.normal(0.0, 0.008, size=n - 1)
    prices = [anchor]
    for r in rets:
        prices.append(round(prices[-1] * float(np.exp(r)), 4))
    start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i, c in enumerate(prices):
        bars.append(
            {
                "id": f"bar-{i}",
                "time": (start + timedelta(hours=i)).isoformat(),
                "price": c,
                "high": round(c * 1.004, 4),
                "low": round(c * 0.996, 4),
                "close": c,
                "volume": 5000,
                "completed_bar": True,
            }
        )
    return bars


def test_paper_store_fast_serialization_is_byte_identical_and_isolates_tampering():
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    broker = PaperBroker(
        Market.BINANCE_USDM,
        "acct-byte",
        (Instrument("NVDAUSDT", "USDT"),),
        {"USDT": D("1000")},
        fee_rate=D("0.0003"),
    )
    store = PaperStore(":memory:", continuous_capacity=True)
    try:
        ctrl = TradingController(
            broker,
            store,
            clock=lambda: now,
            limits=RiskLimits(D("500"), D("500"), D("500"), orders_per_minute=20),
        )
        ctrl.quote(Quote("NVDAUSDT", D("132.89"), now))
        intent = OrderIntent(
            Market.BINANCE_USDM, "NVDAUSDT", Side.BUY, OrderType.LIMIT, D("0.04"), D("132.89"), "ord-1"
        )
        ctrl.submit(intent)
        ctrl.fill("ord-1", D("0.04"), D("132.89"))
        fev = FundingEvent(
            account_id="acct-byte",
            tran_id="t1",
            symbol="NVDAUSDT",
            asset="USDT",
            income_type="FUNDING_FEE",
            amount=D("-0.0123"),
            event_time=now,
            source="test",
            event_id=funding_event_id("FUNDING_FEE", "t1"),
            metadata={"rate": "0.0023"},
        )
        ctrl.replay(fev)

        fast_payload = store._dumps_paper_state(broker.state)
        ref_payload = dumps(broker.state)
        assert fast_payload == ref_payload
        assert decode(json.loads(fast_payload)) == broker.state

        # Mutating in-memory broker.state outside a transaction must not affect authoritative state.
        broker.state.cash["USDT"] = D("1")
        ctrl.quote(Quote("NVDAUSDT", D("133.00"), now))
        assert broker.state.cash["USDT"] != D("1")
        assert store.load() == broker.state
    finally:
        store.close()


def test_grid_execution_authority_cache_matches_uncached_including_unknown():
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    broker = PaperBroker(
        Market.BINANCE_USDM,
        "acct-auth",
        (Instrument("NVDAUSDT", "USDT"),),
        {"USDT": D("1000")},
        fee_rate=D("0.0003"),
    )
    store = PaperStore(":memory:", continuous_capacity=True)
    try:
        ctrl = TradingController(
            broker,
            store,
            clock=lambda: now,
            limits=RiskLimits(D("500"), D("500"), D("500"), orders_per_minute=20),
        )
        cfg = GridConfig(
            mode="arithmetic",
            lower=131.56,
            upper=134.22,
            levels=1,
            initial_cash=1000.0,
            initial_inventory=0,
            initial_price=132.89,
            quantity=1,
            max_inventory=1,
            max_cash_committed=10.0,
            atr_window=3,
            atr_multiplier=1.0,
            rebuild_threshold=2.0,
            min_rebuild_interval=3,
            max_turnover=10.0,
            breakout="pause",
            max_drawdown=0.2,
            minimum_price=0.01,
            minimum_quantity=1,
            fee_bps=3.0,
            slippage_bps=2.0,
            order_ttl=100,
        )
        grid = GridExecution(
            cfg,
            default_demo_rules(),
            deployment_id="dep-auth",
            account_id="acct-auth",
            market=Market.BINANCE_USDM,
            instrument_id="NVDAUSDT",
            initial_equity=D("1000"),
            fee_rate=D("0.0003"),
            continuous_capacity=True,
        )
        obs = {
            "id": "b0",
            "time": "2026-06-01T00:00:00",
            "price": D("132.89"),
            "high": D("133.00"),
            "low": D("132.50"),
            "close": D("132.89"),
            "volume": 5000,
            "completed_bar": True,
        }
        grid.observe_paper(ctrl, obs, delivery=1, observed_at=now)
        key = next(iter(grid.orders))
        grid.submit_paper(ctrl, key, delivery=2)
        grid.mark_unknown_paper(ctrl, key, delivery=3)

        cursor = store.event_cursor()
        cached_hash = grid._authority(broker.state, cursor)
        grid._authority_order_cache.clear()
        uncached_hash = grid._authority(broker.state, cursor)
        assert cached_hash == uncached_hash
    finally:
        store.close()


@pytest.mark.parametrize("strategy_id", ["A_FIXED", "B5", "C5"])
def test_r10_in_memory_vs_disk_wal_exact_financial_equivalence(tmp_path: Path, strategy_id: str):
    bars = _sample_hourly_bars(72)
    start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    funding_events = [
        (start + timedelta(hours=h), D("132.89"), D("0.00025"), "Regular")
        for h in range(0, 72, 8)
    ]
    warmup = [132.5 + 0.05 * ((i % 5) - 2) for i in range(60)]
    fp = FundingPolicy(candidate_id="B5", entry_max_bps_h=5.0, min_net_margin_bps=50.0) if strategy_id == "B5" else None
    vp = VolatilityPolicy(candidate_id="C5") if strategy_id == "C5" else None

    disk_res = run_paired_window(
        tmp_path / f"disk_{strategy_id}",
        strategy_id=strategy_id,
        bars=bars,
        capital=D("10.00"),
        funding_events=funding_events,
        funding_policy=fp,
        vol_policy=vp,
        warmup_closes=warmup,
        account_id=f"eq-{strategy_id}",
        cleanup_dir=False,
    )
    mem_res = run_paired_window(
        tmp_path / f"mem_{strategy_id}",
        strategy_id=strategy_id,
        bars=bars,
        capital=D("10.00"),
        funding_events=funding_events,
        funding_policy=fp,
        vol_policy=vp,
        warmup_closes=warmup,
        account_id=f"eq-{strategy_id}",
        cleanup_dir=True,
    )
    assert mem_res == disk_res
    assert not (tmp_path / f"mem_{strategy_id}").exists()
    assert (tmp_path / f"disk_{strategy_id}" / "paper.sqlite").is_file()


def test_r10_multi_overlay_in_memory_vs_disk_exact_equivalence(tmp_path: Path):
    bars = _sample_hourly_bars(72)
    start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    funding_events = [
        (start + timedelta(hours=h), D("132.89"), D("0.00025"), "Regular")
        for h in range(0, 72, 8)
    ]
    overlays = [
        ("0x", 0.0, None, None),
        ("1x", 1.0, None, None),
        ("demo", 1.0, DEMO_OBSERVED_HOURLY_FUNDING_BPS, None),
    ]
    disk_map = run_paired_window_multi_overlay(
        tmp_path / "disk_multi",
        strategy_id="A_FIXED",
        bars=bars,
        capital=D("10.00"),
        overlays=overlays,
        funding_events=funding_events,
        account_id="eq-multi",
        cleanup_dir=False,
    )
    mem_map = run_paired_window_multi_overlay(
        tmp_path / "mem_multi",
        strategy_id="A_FIXED",
        bars=bars,
        capital=D("10.00"),
        overlays=overlays,
        funding_events=funding_events,
        account_id="eq-multi",
        cleanup_dir=True,
    )
    assert mem_map == disk_map


def test_position_settlements_and_causal_funding_rate_exact_equivalence():
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    trades = [
        {"event_time": (base + timedelta(hours=1)).isoformat(), "side": "BUY", "executed_shares": "0.04"},
        {"event_time": (base + timedelta(hours=8)).isoformat(), "side": "SELL", "executed_shares": "0.02"},
        {"event_time": (base + timedelta(hours=12)).isoformat(), "side": "BUY", "executed_shares": "0.03"},
    ]
    events = [
        (base + timedelta(hours=0), D("130"), D("0.0001"), "Regular"),
        (base + timedelta(hours=8), D("131"), D("0.0002"), "Regular"),
        (base + timedelta(hours=16), D("132"), D("0.0003"), "Regular"),
    ]
    flows = position_settlements(trades, events)
    assert [f["quantity"] for f in flows] == [D("0"), D("0.04"), D("0.05")]
    # Exact tie at hour 8: fill_time == settlement_time -> before=0.04, after=0.02
    assert flows[1]["tie_alternative"] == -D("0.02") * D("131") * D("0.0002")

    # Causal rate with bisect matches naive scan
    times = [e[0] for e in events]
    for h in (0, 7, 8, 16, 30, 50):
        obs = base + timedelta(hours=h)
        fast = causal_funding_rate_bps_h(events, obs, lookback_hours=24, conservative=True, _sorted_times=times)
        naive = causal_funding_rate_bps_h(events, obs, lookback_hours=24, conservative=True, _sorted_times=None)
        assert fast == naive


def test_partition_tail_statistics_matches_full_sort_reference():
    rng = np.random.default_rng(77)
    for n in (50, 200, 1000):
        losses = rng.normal(0.01, 0.08, size=n)
        for conf in (0.95, 0.99):
            var, cvar = _loss_tail(losses, conf)
            # Naive reference
            ref_var = float(np.quantile(losses, conf))
            desc = np.sort(losses)[::-1]
            mass = (1 - conf) * n
            whole = int(np.floor(mass))
            frac = mass - whole
            ref_cvar = float((desc[:whole].sum() + (frac * desc[whole] if frac else 0.0)) / mass)
            assert var == ref_var
            assert cvar == pytest.approx(ref_cvar, rel=0, abs=1e-15)

        res = tail_statistics(losses, confidences=(0.95, 0.99), resamples=99, seed=13)
        # Recompute bootstrap manually with full sort
        rs = np.random.RandomState(13)
        chunk = max(1, min(32, 262144 // n))
        est = np.empty((99, 2))
        for begin in range(0, 99, chunk):
            cnt = min(chunk, 99 - begin)
            desc = np.sort(losses[rs.randint(0, n, size=(cnt, n))], axis=1)[:, ::-1]
            for j, conf in enumerate((0.95, 0.99)):
                mass = (1 - conf) * n
                whole = int(np.floor(mass))
                frac = mass - whole
                est[begin : begin + cnt, j] = (desc[:, :whole].sum(axis=1) + frac * desc[:, whole]) / mass
        for j, conf in enumerate((0.95, 0.99)):
            lo, hi = np.quantile(est[:, j], [0.025, 0.975])
            assert res[str(conf)]["cvar_interval"]["lower"] == pytest.approx(float(lo), rel=0, abs=1e-15)
            assert res[str(conf)]["cvar_interval"]["upper"] == pytest.approx(float(hi), rel=0, abs=1e-15)


def test_s01_persistent_worker_equivalence_and_isolation_enforcement(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "strategy.py").write_text(
        "from decimal import Decimal as D\n"
        "from factorlab.strategies.protocol import Decision, StrategySpec\n"
        "class SimpleTarget:\n"
        "    def __init__(self, threshold='100'):\n"
        "        self.threshold = D(threshold)\n"
        "    def describe(self):\n"
        "        return StrategySpec('simple', '1', 'target', ['NVDAUSDT'], [3600], ['closed_bar'])\n"
        "    def decide(self, event, state):\n"
        "        close = event['bars'][-1]['close']\n"
        "        w = D('0.5') if close >= self.threshold else D('0')\n"
        "        return Decision(target_weight=w, state={'seen': state.get('seen', 0) + 1})\n",
        encoding="utf-8",
    )
    (pkg / "strategy.json").write_text(
        json.dumps({"file": "strategy.py", "class": "SimpleTarget", "version": "1", "parameters": {"threshold": "100"}}),
        encoding="utf-8",
    )

    now = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
    ev1 = {"kind": "closed_bar", "symbol": "NVDAUSDT", "now": now, "event_id": "b1", "bars": [{"close": D("105")}]}
    ev2 = {"kind": "closed_bar", "symbol": "NVDAUSDT", "now": now, "event_id": "b2", "bars": [{"close": D("95")}]}

    one_shot = LoadedStrategy(pkg, trusted=True, persistent_worker=False)
    with LoadedStrategy(pkg, trusted=True, persistent_worker=True, verify_runtime_each_call=False) as persistent:
        assert persistent.spec == one_shot.spec
        assert persistent.binding == one_shot.binding
        d1_a = one_shot.decide(ev1, {})
        d1_b = persistent.decide(ev1, {})
        assert d1_a == d1_b
        d2_a = one_shot.decide(ev2, d1_a.state)
        d2_b = persistent.decide(ev2, d1_b.state)
        assert d2_a == d2_b

    # Verify audit hook still blocks unauthorized filesystem write in persistent mode
    bad_pkg = tmp_path / "bad_pkg"
    bad_pkg.mkdir()
    (bad_pkg / "strategy.py").write_text(
        "from factorlab.strategies.protocol import Decision, StrategySpec\n"
        "class BadStrategy:\n"
        "    def describe(self):\n"
        "        return StrategySpec('bad', '1', 'target', ['NVDAUSDT'], [3600], ['closed_bar'])\n"
        "    def decide(self, event, state):\n"
        "        open('escape.txt', 'w').write('x')\n"
        "        return Decision(state=state)\n",
        encoding="utf-8",
    )
    (bad_pkg / "strategy.json").write_text(
        json.dumps({"file": "strategy.py", "class": "BadStrategy", "version": "1", "parameters": {}}),
        encoding="utf-8",
    )
    with LoadedStrategy(bad_pkg, trusted=True, persistent_worker=True) as bad:
        with pytest.raises(StrategyWorkerError, match="PermissionError"):
            bad.decide(ev1, {})


def test_parallel_helpers_and_blas_pinning():
    assert chunksize_for_tasks(100, 8) >= 1
    assert recommended_workers(1) == 1
    assert recommended_workers(64, cpu_count=28) <= 16
    assert recommended_workers(64, io_heavy=True, cpu_count=28) <= 8
    with create_process_pool(2, single_thread_blas=True) as pool:
        envs = list(pool.map(_worker_read_blas_env, [0, 1]))
    assert all(e["OMP_NUM_THREADS"] == "1" and e["OPENBLAS_NUM_THREADS"] == "1" for e in envs)


def test_paper_store_funding_metadata_mutation_isolation_and_cache_invalidation():
    from factorlab.trading.paper import PaperState

    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    inst = Instrument("NVDAUSDT", "USDT")
    state = PaperState(Market.BINANCE_USDM, "acct-fund-meta", {"NVDAUSDT": inst}, {"USDT": D("100")}, D("0.0004"))
    ev = FundingEvent(
        account_id="acct-fund-meta",
        tran_id="tr-1",
        symbol="NVDAUSDT",
        asset="USDT",
        income_type="FUNDING_FEE",
        amount=D("-0.1234"),
        event_time=now,
        source="test",
        event_id="fe-1",
        metadata={"mark_price": "130.0", "rate": "0.0001"},
    )
    state.funding_events[ev.event_id] = ev

    store = PaperStore(":memory:")
    with store.transaction():
        store.save(state)

    # Mutating loaded event.metadata outside a transaction must not pollute cached state
    loaded_a = store.load()
    loaded_a.funding_events["fe-1"].metadata["mark_price"] = "999.0"
    loaded_b = store.load()
    assert loaded_b.funding_events["fe-1"].metadata["mark_price"] == "130.0"

    # Saving a mutated metadata dict on the same FundingEvent object must invalidate fragment cache
    loaded_a.funding_events["fe-1"].metadata["mark_price"] = "135.5"
    with store.transaction():
        store.save(loaded_a)
    row = store.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
    assert row[0] == dumps(loaded_a)
    assert store.load().funding_events["fe-1"].metadata["mark_price"] == "135.5"
    store.db.close()


def test_slice_window_funding_preserves_predecessor_for_exact_gap_calculation():
    from factorlab.experiments.grid_r10_study import _slice_window_funding

    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    # Sparse events where at win_start (h=48), lookback (24h..48h] has only h=25,
    # while its immediate predecessor is at h=23 (gap_h = 2h, not default 8h).
    all_events = [
        (base + timedelta(hours=15), D("130"), D("0.0002"), "Sparse"),
        (base + timedelta(hours=23), D("130"), D("0.0004"), "Sparse"),
        (base + timedelta(hours=25), D("130"), D("0.0006"), "Sparse"),
        (base + timedelta(hours=52), D("130"), D("0.0005"), "Sparse"),
    ]
    win_start_iso = (base + timedelta(hours=48)).isoformat()
    win_end_iso = (base + timedelta(hours=72)).isoformat()
    sliced = _slice_window_funding(all_events, win_start_iso, win_end_iso, lookback_hours=24)
    for h in (48, 49, 52, 60, 71):
        obs = base + timedelta(hours=h)
        assert (
            causal_funding_rate_bps_h(sliced, obs, lookback_hours=24, conservative=True)
            == causal_funding_rate_bps_h(all_events, obs, lookback_hours=24, conservative=True)
        )
    # Specifically verify h=48 got gap_h=2h (last_bps_h = 6.0 / 2.0 = 3.0), not fallback 8h (0.75)
    est, cnt, last_bps_h = causal_funding_rate_bps_h(sliced, base + timedelta(hours=48), lookback_hours=24)
    assert cnt == 1
    assert last_bps_h == pytest.approx(3.0)
    assert est == pytest.approx(3.0)


def test_s01_persistent_worker_recycles_cleanly_after_error(tmp_path: Path):
    pkg = tmp_path / "err_pkg"
    pkg.mkdir()
    (pkg / "strategy.py").write_text(
        "from decimal import Decimal as D\n"
        "from factorlab.strategies.protocol import Decision, StrategySpec\n"
        "DIRTY = 0\n"
        "class FlakyStrategy:\n"
        "    def describe(self):\n"
        "        return StrategySpec('flaky', '1', 'target', ['NVDAUSDT'], [3600], ['closed_bar'])\n"
        "    def decide(self, event, state):\n"
        "        global DIRTY\n"
        "        if event['event_id'] == 'boom':\n"
        "            DIRTY = 999\n"
        "            raise RuntimeError('injected failure')\n"
        "        return Decision(target_weight=D('0'), state={'dirty': DIRTY})\n",
        encoding="utf-8",
    )
    (pkg / "strategy.json").write_text(
        json.dumps({"file": "strategy.py", "class": "FlakyStrategy", "version": "1", "parameters": {}}),
        encoding="utf-8",
    )
    now = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
    ev_boom = {"kind": "closed_bar", "symbol": "NVDAUSDT", "now": now, "event_id": "boom", "bars": [{"close": D("100")}]}
    ev_ok = {"kind": "closed_bar", "symbol": "NVDAUSDT", "now": now, "event_id": "ok", "bars": [{"close": D("100")}]}
    with LoadedStrategy(pkg, trusted=True, persistent_worker=True, verify_runtime_each_call=False) as strat:
        with pytest.raises(StrategyWorkerError, match="RuntimeError"):
            strat.decide(ev_boom, {})
        # Worker must have been recycled so DIRTY global state from the failed call is gone
        assert strat._worker_proc is None
        decision = strat.decide(ev_ok, {})
        assert decision.state == {"dirty": 0}
