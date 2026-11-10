"""Bounded event entitlements on immutable saved execution paths, not total return."""
import argparse
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path

import numpy as np
import pandas as pd

from factorlab.data.evidence import sha256
from factorlab.errors import SchemaError
from factorlab.strategies.dual_ma_cli import _json
from factorlab.strategies.real_comparison import NAMES
from factorlab.strategies.technical_research import SAFETY


def candidate_entitlements(nav, events):
    """Measure record-close gross rights only; do not post unverified payments.

    Partial event evidence cannot authorize the complete dividend ledger. This
    adapter deliberately has no cash lifecycle or reinvestment implementation.
    """
    dates = pd.DatetimeIndex(nav.date)
    if dates.has_duplicates or not dates.is_monotonic_increasing:
        raise SchemaError("unique ordered close holdings required")
    if not nav.symbol.eq("601857.SH").all():
        raise SchemaError("bounded 601857.SH path required")
    quantities = pd.to_numeric(nav.shares)
    if not np.isfinite(quantities).all() or (quantities < 0).any() or (quantities % 1 != 0).any():
        raise SchemaError("nonnegative integer holdings required")
    ids = [event["event_id"] for event in events]
    if len(set(ids)) != len(ids):
        raise SchemaError("resolve duplicate/revised events explicitly")
    rows = []
    for event in events:
        if event["event_type"] != "CASH_DIVIDEND":
            raise SchemaError("unsupported share-change event; total return blocked")
        record, ex = pd.Timestamp(event["record_date"]), pd.Timestamp(event["ex_date"])
        if not record < ex or record not in dates or ex not in dates:
            raise SchemaError("observed record/ex dates and ordered lifecycle required")
        cash = Decimal(event["cash_per_share"])
        if not cash.is_finite() or cash < 0:
            raise SchemaError("finite nonnegative gross dividend required")
        shares = int(quantities.iloc[dates.get_loc(record)])
        rows.append(dict(event_id=event["event_id"], record_date=event["record_date"],
            ex_date=event["ex_date"], scheduled_pay_date=event["scheduled_pay_date"],
            eligible_shares=shares, cash_per_share=str(cash), gross_entitlement=str(cash * shares),
            basis="BOUNDED_VERIFIED_EVENT_TERMS_ON_SAVED_RECORD_CLOSE_HOLDINGS",
            actual_received=None, tax=None, share_adjustment=None,
            payment_status="ACCOUNT_RECEIPT_UNVERIFIED", tax_status="ACCOUNT_TAX_CONTRACT_UNKNOWN"))
    return rows


def run(saved_root, evidence_path, output):
    root, output = Path(saved_root), Path(output)
    evidence_bytes = Path(evidence_path).read_bytes()
    evidence = json.loads(evidence_bytes)
    payload = (root / "manifest.json").read_bytes()
    if sha256(payload) != evidence["comparison_manifest_sha256"]:
        raise SchemaError("saved comparison manifest identity differs")
    manifest = json.loads(payload)
    if manifest["schema"] != "FACTORLAB_REAL_COMPARISON_V1":
        raise SchemaError("saved P5-R02 comparison required")
    consumed = {}

    def load(name):
        data = (root / name).read_bytes()
        identity = sha256(data)
        if identity != manifest["artifacts_sha256"][name]:
            raise SchemaError("saved execution payload identity differs: " + name)
        consumed[name] = identity
        return pd.read_csv(BytesIO(data), parse_dates=["date"] if name.endswith("_nav.csv") else False)

    config = json.loads((root / "config.json").read_bytes())
    if sha256((root / "config.json").read_bytes()) != manifest["artifacts_sha256"]["config.json"]:
        raise SchemaError("saved configuration identity differs")
    consumed["config.json"] = manifest["artifacts_sha256"]["config.json"]
    initial = config["common"]["initial_cash"]
    summaries, daily = {}, {}
    for name in NAMES:
        nav, trades = load(name + "_nav.csv"), load(name + "_trades.csv")
        load(name + "_signals.csv")
        dates = pd.DatetimeIndex(nav.date)
        if (str(dates[0].date()), str(dates[-1].date()), len(dates)) != ("2018-01-02", "2018-12-28", 243):
            raise SchemaError("saved research window differs")
        signed = trades.trade_notional.where(trades.side.eq("sell"), -trades.trade_notional) - trades.transaction_cost
        flows = signed.groupby(pd.to_datetime(trades.execution_date)).sum()
        table = nav[["date", "shares", "mark_price", "market_value", "cash", "portfolio_value", "transaction_cost"]].copy()
        table["trade_cash_flow"] = table.date.map(flows).fillna(0)
        share_flows = trades.executed_shares.where(trades.side.eq("buy"), -trades.executed_shares)
        table["share_residual"] = table.shares - table.date.map(
            share_flows.groupby(pd.to_datetime(trades.execution_date)).sum()).fillna(0).cumsum()
        table["mark_residual"] = table.market_value - table.shares * table.mark_price
        table["cash_residual"] = table.cash - initial - table.trade_cash_flow.cumsum()
        table["equity_residual"] = table.portfolio_value - table.cash - table.market_value
        if (table[["cash_residual", "equity_residual", "share_residual", "mark_residual"]].abs().to_numpy() > 1e-7).any():
            raise SchemaError("saved execution cash/equity reconciliation failed")
        rights = candidate_entitlements(nav, evidence["events"])
        table["bounded_candidate_gross_rights"] = [
            float(sum((Decimal(row["gross_entitlement"]) for row in rights
                       if pd.Timestamp(row["ex_date"]) <= day), Decimal(0))) for day in table.date]
        # Neither unknown account receipts nor absent events become zero cash.
        for column in ("actual_dividend_received", "dividend_tax", "share_adjustment", "total_equity", "total_return_gross", "total_return_net"):
            table[column] = None
        costs = float(trades.transaction_cost.sum())
        summaries[name] = dict(events=rights, bounded_gross_entitlement=str(sum(
            (Decimal(row["gross_entitlement"]) for row in rights), Decimal(0))),
            raw_price_return_net_costs=float(nav.portfolio_value.iloc[-1] / initial - 1),
            raw_price_pnl_before_explicit_costs=float(nav.portfolio_value.iloc[-1] - initial + costs),
            trading_costs=costs, trading_cost_return_effect=-costs / initial,
            total_return_status="TOTAL_RETURN_UNAVAILABLE", total_return_gross=None, total_return_net=None,
            max_cash_residual=float(table.cash_residual.abs().max()),
            max_equity_residual=float(table.equity_residual.abs().max()))
        daily[name] = table
    output.mkdir(parents=True, exist_ok=False)
    for name, frame in daily.items():
        frame.to_csv(output / (name + "_bounded_accounting.csv"), index=False, float_format="%.17g")
    _json(output / "summary.json", summaries)
    receipt = dict(schema="P6_E01_BOUNDED_ENTITLEMENTS_V1", status="P6_E01_BLOCKED_EVIDENCE",
        safety=SAFETY, total_return_status="TOTAL_RETURN_UNAVAILABLE", total_return=None,
        evidence_sha256=sha256(evidence_bytes), comparison_manifest_sha256=sha256(payload),
        consumed_sha256=consumed, blockers=evidence["blockers"],
        implementation_sha256=sha256(Path(__file__).read_bytes()),
        artifacts_sha256={p.name: sha256(p.read_bytes()) for p in sorted(output.iterdir())})
    _json(output / "manifest.json", receipt)
    return receipt, summaries


def main():
    parser = argparse.ArgumentParser(description="Bounded saved-path rights; NO LIVE MONEY")
    parser.add_argument("--saved-root", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.saved_root, args.evidence, args.output)
