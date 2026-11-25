"""S04 native FACTORLAB_DECISION_V1 integration & equivalence regressions for P8-S02-F01 (F1, F2, F4, F5).

Verifies DualMAUsdmStrategy and CloseMomentumUsdmStrategy through the real S04
StrategyPackage -> LoadedStrategy -> ReleaseConsumer -> DemoStrategyRuntime ->
DemoMarketFeed fake-network order execution path (NO legacy evaluate/v1 proxy,
NO live network calls).
"""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factorlab.integrations.binance.client import Response
from factorlab.integrations.binance_futures_trading.demo_runtime import DemoRuntime
from factorlab.integrations.binance_futures_trading.fake import NOW
from factorlab.integrations.binance_futures_trading.strategy_package import DECISION_INTERFACE, build_package
from factorlab.integrations.binance_futures_trading.strategy_runtime import ClosedBar
from factorlab.strategies.binance_usdm import close_momentum_usdm, dual_ma_usdm
from factorlab.strategies.binance_usdm.close_momentum_usdm import CloseMomentumUsdmStrategy
from factorlab.strategies.binance_usdm.dual_ma_usdm import DualMAUsdmStrategy
from factorlab.trading.target import target_quantity
from test_binance_demo_grid_network import network_grid, writes
from test_binance_demo_network import wired
from test_unified_cloud_strategy_s04 import harness, make_bar, promote_candidate

REPO_ROOT = Path(__file__).resolve().parents[2]
STRAT_DIR = REPO_ROOT / "src" / "factorlab" / "strategies" / "binance_usdm"


def _build_native_usdm_package(
    tmp_path: Path,
    *,
    name: str,
    strategy_cls,
    source_filename: str,
    parameters: dict,
    code_commit: str = "a" * 40,
) -> tuple[Path, dict]:
    inst = strategy_cls(**parameters)
    spec = inst.describe()
    manifest_draft = {
        "schema_version": 1,
        "interface": DECISION_INTERFACE,
        "environment": "BINANCE_USDM_DEMO",
        "symbol": "NVDAUSDT",
        "code_commit": code_commit,
        "strategy_id": spec.strategy_id,
        "strategy_version": spec.version,
        "state_version": spec.state_version,
        "execution_mode": spec.decision_kind,
        "interval_seconds": int(parameters["interval_seconds"]),
        "entrypoint": f"{source_filename[:-3]}:{strategy_cls.__name__}",
        "files": [source_filename],
        "dependencies": [],
        "configuration": parameters,
        "migration": "wait_flat",
        "evidence": {
            "selection_sha256": "1" * 64,
            "candidate_id": f"c-{name}",
            "run_id": f"run-{name}",
            "record_sha256": "2" * 64,
            "financial_admission": "FORMAL_ADMISSION_BLOCKED",
            "expected_active": "legacy-grid",
        },
    }
    archive = tmp_path / f"{name}.zip"
    manifest = build_package(STRAT_DIR, manifest_draft, archive)
    return archive, manifest


def _set_harness_equity(harness, equity_str: str) -> None:
    """Align DemoRuntime continuous authorization opening_equity with the test's fake cash balance."""
    harness.fake.cash = D(equity_str)
    with closing(DemoRuntime(harness.root)) as rt:
        auth = rt.continuous_authorization()
        assert auth is not None
        auth["opening_equity"] = str(D(equity_str))
        with rt.db:
            rt._set("continuous", json.dumps(auth, sort_keys=True))


def _hour_bar(hour_idx: int, close: str, *, interval_seconds: int = 3600, complete: bool = True) -> ClosedBar:
    """Create a 1-indexed hourly bar anchored to fake.NOW so harness.set_time(hour_idx*60, 1) matches."""
    bar = make_bar(hour_idx, close, interval=interval_seconds)
    if complete:
        return bar
    return ClosedBar(
        symbol=bar.symbol,
        opened_at=bar.opened_at,
        closed_at=bar.closed_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        interval_seconds=bar.interval_seconds,
        source=bar.source,
        complete=False,
    )


def test_f1_legacy_fixed_quantity_evaluate_removed_from_new_candidates():
    """F1 P1: New candidates must NOT expose the inequivalent fixed-0.04 evaluate/v1 entrypoint."""
    assert not hasattr(dual_ma_usdm, "evaluate")
    assert not hasattr(close_momentum_usdm, "evaluate")
    assert DualMAUsdmStrategy.s03_protocol == DECISION_INTERFACE
    assert CloseMomentumUsdmStrategy.s03_protocol == DECISION_INTERFACE
    assert DualMAUsdmStrategy.s03_entrypoint == "dual_ma_usdm:DualMAUsdmStrategy"
    assert CloseMomentumUsdmStrategy.s03_entrypoint == "close_momentum_usdm:CloseMomentumUsdmStrategy"


@pytest.mark.parametrize(
    "strategy_cls,source_file,params,closes,equity_str,held_str",
    [
        # DualMA BUY from flat with equity=10, max_weight=0.50 -> target_qty = floor(5/price, 0.01) != hardcoded 0.04!
        (
            DualMAUsdmStrategy,
            "dual_ma_usdm.py",
            {"symbol": "NVDAUSDT", "interval_seconds": 3600, "fast_window": 2, "slow_window": 3, "max_weight": 0.50},
            ["150", "150", "148", "154"],
            "10",
            "0",
        ),
        # DualMA partial-holding top-up: equity=20, max_weight=0.60, held=0.02 -> target capped by max_notional(7) -> delta=+0.02 (rejected by MIN_NOTIONAL=5)
        (
            DualMAUsdmStrategy,
            "dual_ma_usdm.py",
            {"symbol": "NVDAUSDT", "interval_seconds": 3600, "fast_window": 2, "slow_window": 3, "max_weight": 0.60},
            ["150", "150", "148", "154"],
            "20",
            "0.02",
        ),
        # CloseMomentum BUY from flat with equity=10, weight=0.65 -> target_qty matches target_quantity formula
        (
            CloseMomentumUsdmStrategy,
            "close_momentum_usdm.py",
            {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.65, "threshold_bps": 10.0},
            ["150", "152"],
            "10",
            "0",
        ),
        # CloseMomentum SELL reduction from partial holding held=0.03 -> reduce_only SELL 0.03
        (
            CloseMomentumUsdmStrategy,
            "close_momentum_usdm.py",
            {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.90, "threshold_bps": 10.0},
            ["152", "150"],
            "10",
            "0.03",
        ),
    ],
)
def test_f1_research_and_s04_release_position_sizing_equivalence(
    tmp_path, harness, strategy_cls, source_file, params, closes, equity_str, held_str
):
    """F1 P1: Identical frozen strategy source, params, bars, equity, and partial holdings produce
    the EXACT SAME signal, target_weight, target quantity, and S04 fake-network LIMIT order."""
    archive, manifest = _build_native_usdm_package(
        tmp_path, name="equiv", strategy_cls=strategy_cls, source_filename=source_file, parameters=params
    )
    harness.install(archive)
    _set_harness_equity(harness, equity_str)
    harness.fake.quantity = D("0")
    harness.fake.entry = D("0")
    harness.set_time(0, 1)

    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=3600)
        router.store.request(manifest, promote_candidate(manifest, "equiv-activate"))
        router.before_poll()
        assert router.store.snapshot()["active"] == manifest["package_id"]

        book = router.bridge.session._fresh_book()
        ask_px = D(book["askPrice"])
        bid_px = D(book["bidPrice"])

        # Set pre-existing partial position after activation (wait_flat requires flat at switch)
        if D(held_str) > 0:
            harness.fake.quantity = D(held_str)
            harness.fake.entry = bid_px
            router.before_poll()

        # Feed warmup bars historically (zero orders) then last bar live
        for idx, c in enumerate(closes[:-1], start=1):
            bar = _hour_bar(idx, c)
            harness.set_time(idx * 60, 1)
            assert router.historical_bar(bar) is True
        assert len(writes(harness.requests, "POST")) == 0

        last_idx = len(closes)
        last_bar = _hour_bar(last_idx, closes[-1])
        harness.set_time(last_idx * 60, 1)
        assert router.closed_bar(last_bar) is True

        # Compute exact research-side expectation using same strategy class
        research_inst = strategy_cls(**params)
        research_event = {
            "kind": "closed_bar",
            "symbol": "NVDAUSDT",
            "now": last_bar.closed_at.isoformat(),
            "bars": [{"close": c} for c in closes],
        }
        res_dec = research_inst.decide(research_event, {})
        runner = router._load_decision_runner(manifest["package_id"])[2]
        s04_sig = runner._runtime()["last_signal"]
        assert s04_sig["target_weight"] == res_dec.target_weight

        # Deliver live quote -> triggers real S04 fake-network POST /fapi/v1/order
        harness.set_time(last_idx * 60, 2)
        harness.quote(router, str(ask_px))

        # Expected quantity under identical S04 max_notional=7 / max_quantity=0.04 / step=0.01 rules
        step = D("0.01")
        fee_rate = router.bridge.gateway.state.fee_rate
        avail = harness.fake.cash - D(held_str) * bid_px
        raw_t = target_quantity(
            equity=D(equity_str),
            weight=res_dec.target_weight,
            price=ask_px,
            budget_price=ask_px,
            fee_rate=fee_rate,
            current=D(held_str),
            available=max(D("0"), avail),
            step=step,
        )
        ref_px = ask_px
        if raw_t < D(held_str):
            ref_px = bid_px
            raw_t = target_quantity(
                equity=D(equity_str),
                weight=res_dec.target_weight,
                price=bid_px,
                budget_price=bid_px,
                fee_rate=fee_rate,
                current=D(held_str),
                available=max(D("0"), avail),
                step=step,
            )
        capped = min(raw_t, D("7") / ref_px, D("0.04"))
        expected_target = (capped // step) * step
        expected_delta = expected_target - D(held_str)

        posts = writes(harness.requests, "POST")
        assert runner._runtime()["target_position"]["quantity"] == expected_target
        if expected_delta != 0 and (expected_delta < 0 or abs(expected_delta) * ask_px >= D("5")):
            assert len(posts) == 1
            assert posts[0]["side"] == ["BUY" if expected_delta > 0 else "SELL"]
            assert D(posts[0]["quantity"][0]) == abs(expected_delta)
            assert posts[0]["reduceOnly"] == ["true" if expected_delta < 0 else "false"]
        else:
            assert len(posts) == 0
            assert runner._runtime()["last_order"]["reason"] == "MIN_NOTIONAL"


def test_f2_hourly_3600s_strategy_rejected_on_60s_feed_with_zero_posts(tmp_path, harness):
    """F2 P1: Frozen 3600s strategy MUST be rejected on 60s feed (activation, hot-switch, restart,
    historical backfill, unclosed/duplicate/gap bars) with ZERO POST orders, and run normally on 3600s feed."""
    params = {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.90, "threshold_bps": 0.0}
    archive, manifest = _build_native_usdm_package(
        tmp_path, name="mom-1h", strategy_cls=CloseMomentumUsdmStrategy, source_filename="close_momentum_usdm.py", parameters=params
    )
    harness.install(archive)
    harness.set_time(0, 1)

    # 1. Activation on 60s feed MUST fail prepare and roll back to LEGACY with ZERO POSTs
    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=60)
        router.store.request(manifest, promote_candidate(manifest, "wrong-60s-feed"))
        router.before_poll()
        router.before_poll()  # complete rollback transition back to LEGACY
        assert router.store.snapshot()["active"] == "legacy-grid"
        assert router.store.pending() is None
        assert len(writes(harness.requests, "POST")) == 0

    # 2. Activation on legitimate 3600s feed succeeds, then check unclosed/duplicate/gap/60s bars -> ZERO POSTs
    harness.set_time(600, 1)
    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=3600)
        router.store.request(manifest, promote_candidate(manifest, "valid-3600s-feed"))
        router.before_poll()
        assert router.store.snapshot()["active"] == manifest["package_id"]
        runner = router._load_decision_runner(manifest["package_id"])[2]

        # Unclosed hour bar (complete=False) -> ignored, ZERO POSTs
        unclosed = _hour_bar(1, "150", complete=False)
        assert runner.closed_bar(unclosed) is False
        assert len(runner._runtime()["bars"]) == 0

        # Mismatched 60s bar passed directly to historical_bar or closed_bar -> raises ValueError!
        bar_60s = _hour_bar(1, "150", interval_seconds=60)
        with pytest.raises(ValueError, match="interval mismatch"):
            runner.historical_bar(bar_60s)
        with pytest.raises(ValueError, match="interval mismatch"):
            runner.closed_bar(bar_60s)

        # Duplicate & out-of-order & gap bars handled safely without orders
        b1 = _hour_bar(1, "150")
        b2 = _hour_bar(2, "150")
        b4 = _hour_bar(4, "155")  # gap (missing hour 3)
        assert runner.historical_bar(b1) is True
        assert runner.historical_bar(b1) is False  # duplicate ignored
        assert runner.historical_bar(b2) is True
        assert runner.closed_bar(b1) is False  # out-of-order ignored
        assert runner.closed_bar(b4) is False  # gap pauses runtime
        assert runner._runtime()["reason"] == "BAR_SEQUENCE_GAP"
        assert len(writes(harness.requests, "POST")) == 0

    # 3. Restart with feed changed to 60s -> rolls back BEFORE any decision or POST!
    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=60)
        router.before_poll()
        router.before_poll()
        assert router.store.snapshot()["active"] == "legacy-grid"
        assert len(writes(harness.requests, "POST")) == 0


def test_f4_multi_symbol_shared_account_positions_do_not_contaminate_nvda(tmp_path, harness):
    """F4 P2: Positions in QQQUSDT/SPYUSDT/BTCUSDT on a shared Binance account must NEVER be
    counted as NVDAUSDT inventory during BUY entry or SELL reduction."""
    params = {"symbol": "NVDAUSDT", "interval_seconds": 3600, "lookback": 1, "weight": 0.90, "threshold_bps": 0.0}
    archive, manifest = _build_native_usdm_package(
        tmp_path, name="f4-sym", strategy_cls=CloseMomentumUsdmStrategy, source_filename="close_momentum_usdm.py", parameters=params
    )
    harness.install(archive)
    _set_harness_equity(harness, "10")
    harness.fake.quantity = D("0")

    # Inject non-NVDA positions into fake exchange /fapi/v3/account & /fapi/v3/positionRisk responses
    orig_call = harness.fake.__call__

    def _with_other_symbols(request, timeout):
        resp = orig_call(request, timeout)
        if request.path in {"/fapi/v3/account", "/fapi/v3/positionRisk"}:
            data = json.loads(resp.body.decode())
            if isinstance(data, dict) and "positions" in data:
                data["positions"].extend(
                    [
                        {
                            "symbol": "QQQUSDT",
                            "positionAmt": "5.00",
                            "entryPrice": "500",
                            "positionSide": "BOTH",
                            "unrealizedProfit": "0",
                            "initialMargin": "0",
                            "maintMargin": "0",
                        },
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0.50",
                            "entryPrice": "90000",
                            "positionSide": "BOTH",
                            "unrealizedProfit": "0",
                            "initialMargin": "0",
                            "maintMargin": "0",
                        },
                    ]
                )
            elif isinstance(data, list):
                data.extend(
                    [
                        {
                            "symbol": "QQQUSDT",
                            "positionAmt": "5.00",
                            "entryPrice": "500",
                            "markPrice": "500",
                            "positionSide": "BOTH",
                            "notional": "2500",
                            "liquidationPrice": "0",
                            "isolatedMargin": "0",
                        },
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0.50",
                            "entryPrice": "90000",
                            "markPrice": "90000",
                            "positionSide": "BOTH",
                            "notional": "45000",
                            "liquidationPrice": "0",
                            "isolatedMargin": "0",
                        },
                    ]
                )
            return Response(resp.status, resp.headers, json.dumps(data).encode())
        return resp

    harness.fake.__call__ = _with_other_symbols
    harness.set_time(0, 1)

    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=3600)
        router.store.request(manifest, promote_candidate(manifest, "f4-activate"))
        router.before_poll()
        assert router.store.snapshot()["active"] == manifest["package_id"]

        b1 = _hour_bar(1, "150")
        b2 = _hour_bar(2, "152")
        harness.set_time(60, 1)
        assert router.historical_bar(b1) is True
        harness.set_time(120, 1)
        assert router.closed_bar(b2) is True
        harness.set_time(120, 2)
        harness.quote(router, str(router.bridge.session._fresh_book()["askPrice"]))

        posts = writes(harness.requests, "POST")
        # Even though QQQUSDT=5.00 and BTCUSDT=0.50 are held, NVDAUSDT is 0 -> places clean BUY!
        assert len(posts) == 1
        assert posts[0]["symbol"] == ["NVDAUSDT"]
        assert posts[0]["side"] == ["BUY"]
        assert D(posts[0]["quantity"][0]) > D("0")


def test_f5_first_activation_historical_warmup_without_historical_orders(tmp_path, harness):
    """F5 P2: DualMA(fast=5, slow=20) requires history_limit=21 bars. Before warmup -> INSUFFICIENT_DATA
    (zero orders). Historical warmup via S04 historical_bar populates history_limit bars with ZERO
    historical POSTs, and the first live tradable bar matches research identically."""
    params = {"symbol": "NVDAUSDT", "interval_seconds": 3600, "fast_window": 5, "slow_window": 20, "max_weight": 0.90}
    archive, manifest = _build_native_usdm_package(
        tmp_path, name="f5-dma", strategy_cls=DualMAUsdmStrategy, source_filename="dual_ma_usdm.py", parameters=params
    )
    harness.install(archive)
    _set_harness_equity(harness, "10")
    harness.fake.quantity = D("0")

    # 20 warmup bars declining/flat then bar 21 jumps to 250 -> curr_fast=162 > curr_slow=159.5 -> crossover BUY!
    closes = ["160"] * 15 + ["140"] * 5 + ["250"]
    harness.set_time(0, 1)

    with harness.open() as router:
        router.base.supervisor.feed = SimpleNamespace(seconds=3600)
        router.store.request(manifest, promote_candidate(manifest, "f5-activate"))
        router.before_poll()
        runner = router._load_decision_runner(manifest["package_id"])[2]

        # 1. Single bar before warmup -> explicit INSUFFICIENT_DATA, zero POSTs
        b1 = _hour_bar(1, closes[0])
        harness.set_time(60, 1)
        assert router.closed_bar(b1) is True
        assert runner._runtime()["last_signal"]["status"] == "INSUFFICIENT_DATA"
        assert len(writes(harness.requests, "POST")) == 0

        # 2. Deliver remaining 19 historical warmup bars -> ZERO historical POSTs
        for idx in range(2, 21):
            bar = _hour_bar(idx, closes[idx - 1])
            harness.set_time(idx * 60, 1)
            assert router.historical_bar(bar) is True
        assert len(writes(harness.requests, "POST")) == 0
        assert len(runner._runtime()["bars"]) == 20
        assert router.history_watermark() == _hour_bar(20, closes[19]).closed_at

        # 3. First live tradable bar (index 21) triggers exact same BUY crossover as offline research
        b21 = _hour_bar(21, closes[20])
        harness.set_time(21 * 60, 1)
        assert router.closed_bar(b21) is True
        assert runner._runtime()["last_signal"]["status"] == "SIGNAL"
        assert runner._runtime()["last_signal"]["direction"] == "BUY"
        assert runner._runtime()["last_signal"]["target_weight"] == D("0.90")

        harness.set_time(21 * 60, 2)
        harness.quote(router, str(router.bridge.session._fresh_book()["askPrice"]))
        posts = writes(harness.requests, "POST")
        assert len(posts) == 1
        assert posts[0]["side"] == ["BUY"]
        assert D(posts[0]["quantity"][0]) > D("0")
