"""Offline unit tests for FactorLab R08 Demo Grid empirical performance analysis."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from factorlab.experiments.demo_grid_performance import (
    EVIDENCE_BOUNDARIES,
    DEFAULT_OPENING_WALLET,
    DEFAULT_STRATEGY_CAP_USDT,
    run_full_r08_analysis,
)
from factorlab.trading.contracts import (
    FundingEvent,
    Market,
    OrderEvent,
    OrderIntent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
)
from factorlab.trading.paper import OrderRecord, PaperState, Quote
from factorlab.trading.storage import dumps

JID = "eb66189ca89a5d1f8253a4e35c66bd12"
ACCOUNT_ID = "DEMO:FactorLab-Demo:91f3abbe76b43d3fb3b7c83a881ddbac90109ff8ec1201a1dd0cf216660da077"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_synthetic_demo_snapshot(root: Path, *, include_azure_post_handoff: bool) -> Path:
    db_root = root / "databases"
    acct_dir = db_root / JID
    acct_dir.mkdir(parents=True, exist_ok=True)

    # 1. runtime.sqlite3
    rcon = sqlite3.connect(str(db_root / "runtime.sqlite3"))
    rcon.execute("CREATE TABLE runtime(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rcon.execute("CREATE TABLE campaigns(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    rcon.execute(
        "CREATE TABLE dispatches(campaign TEXT NOT NULL, method TEXT NOT NULL, "
        "client_id TEXT NOT NULL, parameters TEXT NOT NULL, receipt TEXT, "
        "PRIMARY KEY (campaign, method, client_id))"
    )
    rcon.execute(
        "INSERT INTO runtime VALUES (?, ?)",
        (
            "continuous",
            json.dumps({"version": 1, "id": "continuous-test", "state": "RUNNING", "journal_id": JID}),
        ),
    )
    rcon.execute(
        "INSERT INTO campaigns VALUES (?, ?)",
        ("smoke-1", json.dumps({"id": "smoke-1", "opening_equity": "5000.00000000", "smoke": True})),
    )
    rcon.execute(
        "INSERT INTO campaigns VALUES (?, ?)",
        ("window-1", json.dumps({"id": "window-1", "opening_equity": "4999.97114760", "smoke": False})),
    )
    dispatches = [
        ("smoke-1", "POST", "fl-smoke-01", {"side": "BUY", "price": "162.94", "quantity": "0.04"}, {"http_status": 200, "order_id": "101"}),
        ("smoke-1", "POST", "fl-smoke-02", {"side": "SELL", "price": "162.30", "quantity": "0.04"}, {"http_status": 200, "order_id": "102"}),
        ("window-1", "POST", "fl-d7345f8795464a65-g-00001-00000001", {"side": "BUY", "price": "129.59", "quantity": "0.04"}, {"http_status": 200, "order_id": "201"}),
    ]
    if include_azure_post_handoff:
        dispatches.extend(
            [
                ("window-1", "POST", "fl-d7345f8795464a65-g-00001-00000006", {"side": "SELL", "price": "132.89", "quantity": "0.04", "reduceOnly": "true"}, {"http_status": 200, "order_id": "202"}),
                ("window-1", "POST", "fl-d7345f8795464a65-g-00002-00000001", {"side": "BUY", "price": "131.56", "quantity": "0.04"}, {"http_status": 200, "order_id": "203"}),
            ]
        )
    for camp, method, cid, params, rcpt in dispatches:
        rcon.execute(
            "INSERT INTO dispatches VALUES (?, ?, ?, ?, ?)",
            (camp, method, cid, json.dumps(params), json.dumps(rcpt)),
        )
    rcon.commit()
    rcon.close()

    # 2. grid-supervisor.sqlite3
    gcon = sqlite3.connect(str(db_root / "grid-supervisor.sqlite3"))
    gcon.execute("CREATE TABLE cursor(id TEXT PRIMARY KEY, bar INTEGER, quote INTEGER, initial_start INTEGER)")
    gcon.execute(
        "CREATE TABLE strategy_control(id INTEGER PRIMARY KEY, configuration TEXT, desired TEXT, report TEXT)"
    )
    dep_active = "fl-d7345f8795464a65-g-00002" if include_azure_post_handoff else "fl-d7345f8795464a65-g-00001"
    gcon.execute(
        "INSERT INTO strategy_control VALUES (1, ?, ?, ?)",
        (
            json.dumps({"interval": "1m", "mode": "execute"}),
            "RUNNING",
            json.dumps(
                {
                    "deployment": {"deployment_id": dep_active},
                    "grid_state": "RUNNING" if include_azure_post_handoff else "PAUSED",
                }
            ),
        ),
    )
    gcon.commit()
    gcon.close()

    # Build canonical orders, events, funding, and state
    t_smoke_buy = datetime(2026, 11, 14, 11, 15, 52, tzinfo=timezone.utc)
    t_smoke_sell = datetime(2026, 11, 14, 11, 30, 11, tzinfo=timezone.utc)
    t_g1_buy = datetime(2026, 11, 22, 5, 18, 48, tzinfo=timezone.utc)
    t_fund_1 = datetime(2026, 11, 22, 9, 0, 0, tzinfo=timezone.utc)
    t_fund_2 = datetime(2026, 11, 22, 16, 0, 0, tzinfo=timezone.utc)
    t_g1_exit = datetime(2026, 11, 22, 16, 24, 30, 811000, tzinfo=timezone.utc)
    t_g2_buy = datetime(2026, 11, 22, 16, 25, 3, 471000, tzinfo=timezone.utc)

    def _make_record(cid, ex_id, side, price, filled, fee, status, ts, reduce_only=False):
        intent = OrderIntent(
            client_order_id=cid,
            instrument_id="NVDAUSDT",
            market=Market.BINANCE_USDM,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.04"),
            limit_price=Decimal(price),
            reduce_only=reduce_only,
        )
        snap = OrderSnapshot(
            intent=intent,
            exchange_order_id=ex_id,
            status=status,
            cumulative_filled_quantity=Decimal(filled),
            cumulative_fee=Decimal(fee),
        )
        return OrderRecord(
            snapshot=snap,
            confirmed_status=status,
            reservation_price=Decimal(price),
            observed_at=ts,
        )

    orders_map = {
        "fl-smoke-01": _make_record("fl-smoke-01", "101", Side.BUY, "162.94000", "0.04", "0.00162940", OrderStatus.FILLED, t_smoke_buy),
        "fl-smoke-02": _make_record("fl-smoke-02", "102", Side.SELL, "162.30000", "0.04", "0.00162300", OrderStatus.FILLED, t_smoke_sell, reduce_only=True),
        "fl-d7345f8795464a65-g-00001-00000001": _make_record(
            "fl-d7345f8795464a65-g-00001-00000001", "201", Side.BUY, "129.59000", "0.04", "0", OrderStatus.FILLED, t_g1_buy
        ),
    }
    if include_azure_post_handoff:
        orders_map["fl-d7345f8795464a65-g-00001-00000006"] = _make_record(
            "fl-d7345f8795464a65-g-00001-00000006", "202", Side.SELL, "132.89000", "0.04", "0", OrderStatus.FILLED, t_g1_exit, reduce_only=True
        )
        orders_map["fl-d7345f8795464a65-g-00002-00000001"] = _make_record(
            "fl-d7345f8795464a65-g-00002-00000001", "203", Side.BUY, "131.56000", "0.00", "0", OrderStatus.SUBMITTED, t_g2_buy
        )

    funding_list = [
        FundingEvent(
            account_id=ACCOUNT_ID,
            tran_id="10001",
            symbol="NVDAUSDT",
            asset="USDT",
            income_type="FUNDING_FEE",
            amount=Decimal("-0.04000000"),
            event_time=t_fund_1,
            source="binance-rest-income",
            event_id="funding:FUNDING_FEE:10001",
            metadata={"endpoint": "/fapi/v1/income"},
        )
    ]
    if include_azure_post_handoff:
        funding_list.append(
            FundingEvent(
                account_id=ACCOUNT_ID,
                tran_id="10002",
                symbol="NVDAUSDT",
                asset="USDT",
                income_type="FUNDING_FEE",
                amount=Decimal("-0.05763384"),
                event_time=t_fund_2,
                source="binance-rest-income",
                event_id="funding:FUNDING_FEE:10002",
                metadata={"endpoint": "/fapi/v1/income"},
            )
        )

    # 3. protocol.sqlite3
    pcon = sqlite3.connect(str(acct_dir / "protocol.sqlite3"))
    pcon.execute("CREATE TABLE futures_orders(id TEXT PRIMARY KEY, payload TEXT NOT NULL, quote TEXT NOT NULL)")
    pcon.execute("CREATE TABLE futures_receipts(id TEXT PRIMARY KEY, client_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL)")
    pcon.execute(
        "CREATE TABLE futures_order_recovery(client_order_id TEXT PRIMARY KEY, symbol TEXT, exchange_order_id TEXT, "
        "raw_status TEXT, confirmed_status TEXT, confirmed_filled_qty TEXT, confirmed_quote TEXT, confirmed_fee TEXT)"
    )
    pcon.execute(
        "CREATE TABLE futures_funding(id TEXT PRIMARY KEY, tran_id TEXT NOT NULL, event_time_ms INTEGER NOT NULL, "
        "payload TEXT NOT NULL, raw_payload TEXT NOT NULL, observed_at TEXT NOT NULL)"
    )
    pcon.execute(
        "CREATE TABLE futures_income_coverage(scope TEXT PRIMARY KEY, covered_from_ms INTEGER NOT NULL, "
        "covered_through_ms INTEGER NOT NULL, complete INTEGER NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL)"
    )
    pcon.execute("CREATE TABLE futures_rejections(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    pcon.execute("CREATE TABLE futures_meta(id INTEGER PRIMARY KEY, account TEXT NOT NULL, reconciled INTEGER NOT NULL)")
    pcon.execute("INSERT INTO futures_meta VALUES (1, ?, 1)", (ACCOUNT_ID,))

    quote_map = {
        "fl-smoke-01": "6.5176000",
        "fl-smoke-02": "6.4920000",
        "fl-d7345f8795464a65-g-00001-00000001": "5.1836000",
        "fl-d7345f8795464a65-g-00001-00000006": "5.3156000",
        "fl-d7345f8795464a65-g-00002-00000001": "0",
    }
    receipt_specs = [
        ("rcpt-1", "fl-smoke-01", "101", "BUY", "162.94000", "0.04", "6.5176000", "0.00162940", "0", False, t_smoke_buy, 1),
        ("rcpt-2", "fl-smoke-02", "102", "SELL", "162.30000", "0.04", "6.4920000", "0.00162300", "-0.02560000", False, t_smoke_sell, 2),
        ("rcpt-3", "fl-d7345f8795464a65-g-00001-00000001", "201", "BUY", "129.59000", "0.04", "5.1836000", "0", "0", True, t_g1_buy, 3),
    ]
    if include_azure_post_handoff:
        receipt_specs.extend(
            [
                ("rcpt-4", "fl-d7345f8795464a65-g-00001-00000006", "202", "SELL", "132.89000", "0.04", "5.3156000", "0", "0.13200000", True, t_g1_exit, 4),
                ("rcpt-5", "fl-d7345f8795464a65-g-00002-00000001", "203", "BUY", "131.56000", "0.00", "0", "0", "0", True, t_g2_buy, None),
            ]
        )
    for cid, rec in orders_map.items():
        pcon.execute("INSERT INTO futures_orders VALUES (?, ?, ?)", (cid, dumps(rec), quote_map[cid]))
        pcon.execute(
            "INSERT INTO futures_order_recovery VALUES (?, 'NVDAUSDT', ?, ?, ?, ?, ?, ?)",
            (
                cid,
                rec.snapshot.exchange_order_id,
                rec.confirmed_status.value,
                rec.confirmed_status.value,
                str(rec.snapshot.cumulative_filled_quantity),
                quote_map[cid],
                str(rec.snapshot.cumulative_fee),
            ),
        )
    for rid, cid, ex_id, side, px, qty, cumq, comm, rpnl, maker, ts, tid in receipt_specs:
        ms = int(ts.timestamp() * 1000)
        trades = (
            [
                {
                    "id": tid,
                    "orderId": int(ex_id),
                    "symbol": "NVDAUSDT",
                    "side": side,
                    "price": px,
                    "qty": qty,
                    "quoteQty": cumq,
                    "commission": comm,
                    "commissionAsset": "USDT",
                    "realizedPnl": rpnl,
                    "maker": maker,
                    "time": ms,
                }
            ]
            if tid is not None
            else []
        )
        payload = {
            "source": "GET",
            "order": {
                "orderId": int(ex_id),
                "clientOrderId": cid,
                "symbol": "NVDAUSDT",
                "side": side,
                "status": "FILLED" if tid is not None else "NEW",
                "price": px,
                "origQty": "0.04",
                "executedQty": qty,
                "cumQuote": cumq,
                "time": ms - 155 if cid.endswith("00000006") else ms,
                "updateTime": ms,
            },
            "trades": trades,
        }
        pcon.execute(
            "INSERT INTO futures_receipts VALUES (?, ?, ?, ?)",
            (rid, cid, ts.isoformat(), json.dumps(payload)),
        )
    for fev in funding_list:
        ms = int(fev.event_time.timestamp() * 1000)
        pcon.execute(
            "INSERT INTO futures_funding VALUES (?, ?, ?, ?, '{}', ?)",
            (fev.event_id, fev.tran_id, ms, dumps(fev), fev.event_time.isoformat()),
        )
    pcon.execute(
        "INSERT INTO futures_income_coverage VALUES ('FUNDING_FEE', 1794650735948, 1795365886860, 1, '2026-11-22T16:44:47+00:00', '{}')"
    )
    pcon.commit()
    pcon.close()

    # 4. controller.sqlite3
    ccon = sqlite3.connect(str(acct_dir / "controller.sqlite3"))
    ccon.execute("CREATE TABLE snapshot(id INTEGER PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL)")
    ccon.execute("CREATE TABLE events(id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    total_funding = sum((f.amount for f in funding_list), Decimal("0"))
    final_cash = (
        Decimal("5000.00551376")
        if include_azure_post_handoff
        else Decimal("4999.97114760") + total_funding
    )
    state = PaperState(
        market=Market.BINANCE_USDM,
        account_id=ACCOUNT_ID,
        instruments={},
        fee_rate=Decimal("0"),
        orders=orders_map,
        positions={},
        quotes={"NVDAUSDT": Quote("NVDAUSDT", Decimal("132.73000"), datetime(2026, 11, 22, 16, 44, 46, tzinfo=timezone.utc))},
        cash={"USDT": final_cash},
        funding={"USDT": total_funding},
        funding_events={f.event_id: f for f in funding_list},
    )
    ccon.execute("INSERT INTO snapshot VALUES (1, 1, ?)", (dumps(state),))

    # Deployments
    dep1 = {
        "deployment_id": "fl-d7345f8795464a65-g-00001",
        "initial_equity": "4999.97114760",
        "config": {"lower": 129.59, "upper": 132.21, "initial_price": 130.9, "levels": 1, "max_cash_committed": 7},
        "rules": {"quantity_unit": "0.04"},
    }
    ccon.execute("INSERT INTO events VALUES (?, ?)", ("demo-grid:fl-d7345f8795464a65-g-00001:deployment", dumps(dep1)))
    if include_azure_post_handoff:
        dep2 = {
            "deployment_id": "fl-d7345f8795464a65-g-00002",
            "initial_equity": "5000.00551376",
            "config": {"lower": 131.56, "upper": 134.22, "initial_price": 132.89, "levels": 1, "max_cash_committed": 7},
            "rules": {"quantity_unit": "0.04"},
        }
        ccon.execute("INSERT INTO events VALUES (?, ?)", ("demo-grid:fl-d7345f8795464a65-g-00002:deployment", dumps(dep2)))

    # Order events
    oe_list = [
        OrderEvent("fl-smoke-01", "101", OrderStatus.FILLED, Decimal("0.04"), Decimal("162.94000"), t_smoke_buy, "binance-rest", "rest:smoke-01", Decimal("0.00162940")),
        OrderEvent("fl-smoke-02", "102", OrderStatus.FILLED, Decimal("0.04"), Decimal("162.30000"), t_smoke_sell, "binance-rest", "rest:smoke-02", Decimal("0.00162300")),
        OrderEvent("fl-d7345f8795464a65-g-00001-00000001", "201", OrderStatus.FILLED, Decimal("0.04"), Decimal("129.59000"), t_g1_buy, "binance-rest-recovery", "recovery:g1-buy", Decimal("0")),
    ]
    if include_azure_post_handoff:
        oe_list.append(
            OrderEvent("fl-d7345f8795464a65-g-00001-00000006", "202", OrderStatus.FILLED, Decimal("0.04"), Decimal("132.89000"), t_g1_exit, "binance-rest", "rest:g1-exit", Decimal("0"))
        )
    for oe in oe_list:
        ccon.execute("INSERT INTO events VALUES (?, ?)", (oe.event_id, dumps(oe)))
    for fev in funding_list:
        ccon.execute("INSERT INTO events VALUES (?, ?)", (fev.event_id, dumps(fev)))

    # Minimal 1m bars for Gen 1 covering buy, dip, breakout (08:42Z), funding hours, and exit
    bar_points = [
        (datetime(2026, 11, 21, 19, 1, tzinfo=timezone.utc), "130.90", "130.85", "130.85"),
        (datetime(2026, 11, 22, 5, 18, tzinfo=timezone.utc), "129.65", "129.55", "129.59"),
        (datetime(2026, 11, 22, 5, 40, tzinfo=timezone.utc), "129.50", "129.45", "129.45"),
        (datetime(2026, 11, 22, 8, 42, tzinfo=timezone.utc), "132.36", "131.99", "132.36"),
        (datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc), "132.40", "132.25", "132.33"),
        (datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc), "132.95", "132.80", "132.86"),
        (datetime(2026, 11, 22, 16, 24, tzinfo=timezone.utc), "132.91", "132.84", "132.91"),
    ]
    for i, (bt, hi, lo, cl) in enumerate(bar_points, start=1):
        cmd = {
            "action": "observe",
            "value": {
                "id": f"bar-{i}",
                "time": bt.replace(tzinfo=None).isoformat(),
                "completed_bar": True,
                "high": hi,
                "low": lo,
                "close": cl,
                "source_bar": {"open": cl, "volume": "50"},
            },
        }
        ccon.execute("INSERT INTO events VALUES (?, ?)", (f"demo-grid:fl-d7345f8795464a65-g-00001:command:{i}", dumps(cmd)))
    if include_azure_post_handoff:
        ccon.execute(
            "INSERT INTO events VALUES (?, ?)",
            ("demo-grid:fl-d7345f8795464a65-g-00001:command:1295", dumps({"action": "exit_limit", "value": {"price": "132.89", "quantity": "0.04"}})),
        )
    ccon.commit()
    ccon.close()

    # Side metadata files
    if include_azure_post_handoff:
        (root / "cloud-health.json").write_text(
            json.dumps(
                {
                    "nav_usdt": "5000.00551376",
                    "unknown_orders": [],
                    "grid": {"deployment_id": "fl-d7345f8795464a65-g-00002"},
                    "campaign": {"post_budget": 4, "posts_consumed": 2, "remaining_posts": 2},
                }
            ),
            encoding="utf-8",
        )
        (root / "execute-handoff-result.json").write_text(
            json.dumps({"reconciled_at": "2026-11-22T16:23:44Z", "snapshot_verified": True}),
            encoding="utf-8",
        )
        ops_dir = root / "ops"
        ops_dir.mkdir(exist_ok=True)
        (ops_dir / "r08-sys-exec.txt").write_text("MainPID=3792\nNRestarts=0\nActiveState=active\n", encoding="utf-8")
        (ops_dir / "r08-journal-execute.log").write_text(
            '2026-11-22T16:24:16+00:00 host systemd[1]: Started factorlab-demo-execute.service\n'
            '2026-11-22T16:25:48+00:00 host factorlab-demo[3323]: {"requests": {"GET": 362, "POST": 2, "DELETE": 0}, "deployment": {"deployment_id": "fl-d7345f8795464a65-g-00002"}}\n'
            '2026-11-22T16:25:49+00:00 host systemd[1]: Started factorlab-demo-execute.service\n'
            '2026-11-22T16:44:47+00:00 host factorlab-demo[3792]: {"requests": {"GET": 500, "POST": 0, "DELETE": 0}, "deployment": {"deployment_id": "fl-d7345f8795464a65-g-00002"}}\n',
            encoding="utf-8",
        )
    return root


def test_r08_readonly_dedup_attribution_and_report(tmp_path: Path):
    win_dir = _build_synthetic_demo_snapshot(tmp_path / "win", include_azure_post_handoff=False)
    az_dir = _build_synthetic_demo_snapshot(tmp_path / "az", include_azure_post_handoff=True)
    out_dir = tmp_path / "out"

    # Hash all source SQLite files before running to prove strictly read-only access
    source_dbs = sorted(list((win_dir / "databases").rglob("*.sqlite3")) + list((az_dir / "databases").rglob("*.sqlite3")))
    before_hashes = {p: _sha256(p) for p in source_dbs}

    analysis = run_full_r08_analysis(
        az_dir,
        windows_snapshot=win_dir,
        output_dir=out_dir,
        opening_wallet=DEFAULT_OPENING_WALLET,
        strategy_cap_usdt=DEFAULT_STRATEGY_CAP_USDT,
    )

    after_hashes = {p: _sha256(p) for p in source_dbs}
    assert before_hashes == after_hashes
    assert not list((az_dir / "databases").rglob("*-wal"))

    # Deduplication & cross-DB consistency checks
    assert analysis["dedup_audit"]["cloud_is_strict_superset"] is True
    assert analysis["dedup_audit"]["protocol_orders"]["windows_only"] == 0
    assert analysis["dedup_audit"]["protocol_orders"]["cloud_only_new"] == 2
    assert analysis["consistency"]["all_checks_passed"] is True
    assert analysis["ops_evidence"]["execute_journal"]["zero_duplicate_post_after_restart"] is True

    # Authoritative FIFO reconciliation & cohort separation
    emp = analysis["empirical"]
    fifo = emp["fifo_reconciliation"]
    assert fifo["reconciled_within_quantum"] is True
    assert fifo["reconciliation_residual_usdt"] == "0.00000000"
    assert fifo["final_equity_usdt"] == "5000.00551376"
    assert fifo["cloud_health_nav_difference_usdt"] == "0.00000000"
    assert fifo["completed_grid_cycles"] == 0
    assert fifo["risk_exit_gross_usdt"] == "0.13200000"

    assert emp["cohorts"]["pre_grid_smoke"]["net_pnl_usdt"] == "-0.02885240"
    assert emp["cohorts"]["all_grid"]["net_pnl_usdt"] == "0.03436616"
    assert emp["cohorts"]["full_account"]["net_pnl_usdt"] == "0.00551376"

    # Explicit 3-capital-basis discipline
    cap = emp["capital_basis_metrics"]
    assert cap["strategy_committed_cap_base"]["capital_usdt"] == "7.00000000"
    assert abs(cap["strategy_committed_cap_base"]["grid_net_return_pct"] - 0.4909) < 0.001
    assert abs(cap["peak_deployed_notional_base"]["grid_net_return_pct"] - 0.6630) < 0.001
    assert cap["account_equity_base"]["effective_peak_leverage_x"] < 0.002

    # F11 & F08 attribution
    f11 = emp["f11_f08_analysis"]["f11_breakout_exit"]
    assert f11["exit_fill_price"] == "132.89000"
    assert f11["actual_net_pnl_usdt"] == "0.03436616"
    assert f11["delay_counterfactual"]["first_breakout_close"] == "132.36000"
    assert emp["f11_f08_analysis"]["f08_rotation"]["to_deployment"] == "fl-d7345f8795464a65-g-00002"

    # Benchmarks generated & include R06 parity and funding comparison
    bench_names = {b["benchmark_name"] for b in analysis["benchmarks"]}
    assert "NVDAUSDT_BH_0.04_EX_FUNDING" in bench_names
    assert "NVDAUSDT_BH_0.04_WITH_OBSERVED_FUNDING" in bench_names
    assert "R06_EXPOSURE_CONTROL_0.04_HOURLY_SIM" in bench_names

    # Output files & sanitization checks
    report_text = (out_dir / "REPORT.md").read_text(encoding="utf-8")
    for tag in EVIDENCE_BOUNDARIES.values():
        assert tag in report_text
    assert ACCOUNT_ID not in report_text
    assert "91f3ab...0da077" in report_text
    assert "85.211.193.242" not in report_text
    assert (out_dir / "summary.json").is_file()
    assert (out_dir / "orders_lifecycle.csv").is_file()
    assert (out_dir / "fills_and_funding.csv").is_file()
    assert (out_dir / "generations.csv").is_file()
    assert (out_dir / "benchmarks.csv").is_file()


def test_r08_tail_risk_and_stress_smoke(tmp_path: Path):
    import numpy as np
    import pandas as pd
    from factorlab.experiments.demo_grid_performance import run_tail_risk_analysis

    snap_dir = tmp_path / "r04b_mini"
    snap_dir.mkdir(parents=True)
    rng = np.random.default_rng(42)
    n_bars = 240
    times = pd.date_range("2026-05-14T00:00:00Z", periods=n_bars, freq="h")
    closes = 130.0 * np.exp(np.cumsum(rng.normal(0.0, 0.005, size=n_bars)))
    df = pd.DataFrame(
        {
            "open_time": times,
            "close_time": times + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1),
            "open": closes,
            "high": closes * 1.003,
            "low": closes * 0.997,
            "close": closes,
            "volume": np.full(n_bars, 200.0),
        }
    )
    bars_path = snap_dir / "bars.parquet"
    df.to_parquet(bars_path, index=False)
    bars_sha = hashlib.sha256(bars_path.read_bytes()).hexdigest()
    manifest = {
        "source": "SYNTHETIC_UNIT_TEST",
        "contract": {"instrument_id": "BINANCE:USDM:NVDAUSDT"},
        "interval": "1h",
        "data_version": "test-v1",
        "acquired_at": "2026-06-01T00:00:00+00:00",
        "integrity": {"complete": True},
        "range": {
            "start_ms": int(times[0].value // 10**6),
            "end_ms": int((times[-1] + pd.Timedelta(hours=1)).value // 10**6),
        },
        "tables": {"bars": {"path": "bars.parquet", "sha256": bars_sha}},
        "raw_receipts": [
            {
                "endpoint": "klines",
                "params": {"interval": "1h"},
                "url": "https://example.invalid/klines",
                "sha256": bars_sha,
                "acquired_at": "2026-06-01T00:00:00+00:00",
            }
        ],
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    tail = run_tail_risk_analysis(
        snap_dir,
        anchor_price=132.89,
        n_paths=4,
        n_steps=24,
        seed=808,
        observed_demo_hourly_funding_bps=22.97,
        max_workers=2,
    )
    assert tail["evidence_class"] == EVIDENCE_BOUNDARIES["synthetic_simulation"]
    scenario_keys = {s["scenario"] for s in tail["stochastic_scenarios"]}
    assert {"gbm_low_vol_0.5x", "gbm_base_vol_1.0x", "gbm_high_vol_2.0x", "moving_block_bootstrap_24h", "jump_stress_downside"}.issubset(scenario_keys)
    assert len(tail["deterministic_stress"]) == 6
    assert "NO LIVE MONEY" in tail["boundaries"]
