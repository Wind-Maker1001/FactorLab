"""Deterministic grid experiments and replayable synthetic research evidence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import math
from pathlib import Path

import pandas as pd

from factorlab._identity import source_context_root
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.experiments.provenance import capture_provenance
from factorlab.dataset.identity import file_hash, json_hash
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, compare_results
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.strategies.grid import GridConfig, GridStrategy


SCHEMA = "FACTORLAB_SYNTHETIC_GRID_V1"
BOUNDARY = ["SOFTWARE_ONLY", "SYNTHETIC", "NO LIVE MONEY", "PIT_UNVERIFIED",
            "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED"]


def synthetic_scenarios():
    """Return four fixed 96-session paths without random state or external data."""
    days = pd.bdate_range("2024-01-02", periods=96)
    paths = {
        "range": [100 + 9 * math.sin(i * math.pi / 8) for i in range(96)],
        "rising": [100 + .65 * i for i in range(96)],
        "falling": [100 - .65 * i for i in range(96)],
        "gap": [100 + 4 * math.sin(i * math.pi / 6) if i < 40
                else 72 + 3 * math.sin((i - 40) * math.pi / 7) for i in range(96)],
    }
    return {name: [dict(id=f"{name}-{i:03d}", time=day.date().isoformat(),
                        price=float(price), volume=10000, completed_bar=True,
                        high=float(price + 1), low=float(price - 1), close=float(price))
                   for i, (day, price) in enumerate(zip(days, prices))]
            for name, prices in paths.items()}


def _fees(costs):
    return FeeSchedule([FeeRule(effective_from=date(1900, 1, 1),
        commission_bps=costs["fee_bps"], minimum_commission_cny=0,
        slippage_bps=costs["slippage_bps"])])


def _buyhold(events, config, fees):
    price = events[0]["price"]
    unit_cost = price + fees.calculate(events[0]["time"], "buy", price).total
    bought = math.floor(config.initial_cash / unit_cost)
    notional = bought * price
    cost = fees.calculate(events[0]["time"], "buy", notional)
    cash = config.initial_cash - notional - cost.total
    inventory = config.initial_inventory + bought
    return [dict(time=event["time"], equity=cash + inventory * event["price"],
                 cash=cash, inventory=inventory, reserved_cash=0, reserved_inventory=0,
                 realized_pnl=0, realized_grid_pnl=0, unrealized_pnl=inventory * event["price"] - (
                     config.initial_inventory * config.initial_price + notional + cost.total),
                 committed=config.initial_inventory * config.initial_price + notional + cost.total,
                 fees=cost.total - cost.slippage, slippage=cost.slippage,
                 turnover=notional / (config.initial_cash + config.initial_inventory * config.initial_price))
            for event in events]


def _nav(snapshots, benchmark, origin):
    frame = pd.DataFrame(snapshots)
    strategy_nav = frame.equity / origin
    benchmark_nav = pd.Series([row["equity"] / origin for row in benchmark])
    costs = frame.fees + frame.slippage
    return pd.DataFrame(dict(date=pd.to_datetime(frame.time),
        strategy_nav=strategy_nav, benchmark_nav=benchmark_nav,
        strategy_return=strategy_nav.pct_change().fillna(strategy_nav.iloc[0] - 1),
        benchmark_return=benchmark_nav.pct_change().fillna(benchmark_nav.iloc[0] - 1),
        transaction_cost=costs.diff().fillna(costs.iloc[0]),
        turnover=frame.turnover.diff().fillna(frame.turnover.iloc[0]),
        holdings=(frame.inventory != 0).astype(int)))


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")
    return {"path": str(path.resolve()), "sha256": file_hash(path)}


def run_experiment(output, *, replay=None):
    """Write a fresh evidence directory; replay consumes its saved manifest.

    Comparison groups are per scenario, never across different price paths.
    Costs are a symmetric synthetic commission plus cash slippage, not an
    exchange-specific fee schedule. No fitting or model selection occurs.
    """
    if replay is None:
        configs = {mode: asdict(GridConfig(mode=mode, lower=80, upper=120, levels=9,
            initial_cash=10000, initial_inventory=20, initial_price=100,
            quantity=5, max_inventory=200, max_cash_committed=10000,
            atr_window=8, atr_multiplier=2, rebuild_threshold=2,
            min_rebuild_interval=5, max_turnover=10, breakout="rebuild",
            max_drawdown=.3, minimum_price=.01, minimum_quantity=1,
            fee_bps=3, slippage_bps=5)) for mode in ("arithmetic", "geometric", "atr")}
        manifest = dict(schema=SCHEMA, evidence=BOUNDARY, scenarios=synthetic_scenarios(), configs=configs,
                        costs=dict(fee_bps=3, slippage_bps=5))
    else:
        manifest = json.loads(Path(replay).read_text(encoding="utf-8"))
        if manifest["schema"] != SCHEMA:
            raise ValueError("unsupported synthetic grid manifest schema")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest_artifact = _write(output / "manifest.json", manifest)
    provenance = capture_provenance(source_context_root())
    tracker = ExperimentTracker(output / "tracker")
    fees = _fees(manifest["costs"])
    summaries = []
    for scenario, events in sorted(manifest["scenarios"].items()):
        directory = output / scenario
        directory.mkdir()
        configs = {name: GridConfig(**values) for name, values in sorted(manifest["configs"].items())}
        reference = next(iter(configs.values()))
        origin = reference.initial_cash + reference.initial_inventory * reference.initial_price
        baseline = _buyhold(events, reference, fees)
        calendar = tuple(event["time"] for event in events)
        sample_hash = json_hash(events)
        context = EvaluationContext(dataset_id=sample_hash, universe_id="synthetic-single-asset",
            label="not_used", label_id="not_used", benchmark_id="buyhold-" + sample_hash,
            calendar=calendar, holding_days=1, source_kind="SYNTHETIC")
        evaluation = ResearchEvaluation(context)
        results, run_ids = [], []
        arms = [("buyhold", baseline, None)]
        for name, config in configs.items():
            if (config.initial_cash, config.initial_inventory, config.initial_price) != (
                    reference.initial_cash, reference.initial_inventory, reference.initial_price):
                raise ValueError("comparison arms require identical starting endowments")
            strategy = GridStrategy(config, fees=fees)
            for event in events:
                strategy.on_event(dict(event))
            arms.append((name, strategy.snapshots, strategy))
        for name, snapshots, strategy in arms:
            nav = _nav(snapshots, baseline, origin)
            result = (evaluation.benchmark(name, nav) if name == "buyhold"
                      else evaluation.portfolio(name, nav))
            results.append(result)
            artifacts = {"manifest": manifest_artifact,
                "snapshots": _write(directory / (name + "-snapshots.json"), snapshots),
                "evaluation": _write(directory / (name + "-evaluation.json"),
                    dict(result.to_dict(), evidence=BOUNDARY))}
            if strategy is not None:
                artifacts["audit"] = _write(directory / (name + "-audit.json"), strategy.events)
                artifacts["orders"] = _write(directory / (name + "-orders.json"),
                    {key: asdict(order) for key, order in strategy.orders.items()})
                artifacts["replay"] = _write(directory / (name + "-replay.json"),
                    strategy.replay_payload())
            identifier = scenario + "-" + name
            definition = dict(model=name, parameters=manifest["configs"].get(name, {
                "allocation": "buy maximum whole units at first event; hold"}), factors=[],
                dataset=dict(source_kind="SYNTHETIC", scenario=scenario, sample_hash=sample_hash),
                label=dict(kind="portfolio_equity"),
                windows=dict(train=None, validation=None, test=[calendar[0], calendar[-1]]),
                walk_forward=dict(kind="not_applicable_no_fitting"), costs=manifest["costs"])
            tracker.create_experiment(identifier, definition)
            metrics = {key: metric.value for key, metric in result.metrics.items()}
            final = snapshots[-1]
            diagnostics = {key: final[key] for key in (
                "realized_pnl", "realized_grid_pnl", "unrealized_pnl", "fees", "slippage", "turnover")}
            diagnostics.update(fills=int(baseline[0]["turnover"] > 0) if strategy is None else
                sum(event["kind"] == "TRADE" for event in strategy.events),
                max_inventory=max(row["inventory"] for row in snapshots) if strategy is None else strategy.max_inventory_observed,
                max_committed=max(row["committed"] for row in snapshots) if strategy is None else strategy.max_committed_observed,
                max_reserved_cash=0 if strategy is None else strategy.max_reserved_cash_observed,
                rebuilds=0 if strategy is None else strategy.rebuilds)
            for status in ("OPEN", "CANCELLED", "REJECTED"):
                diagnostics[status + "_orders"] = 0 if strategy is None else sum(
                    order.status in ({"OPEN", "PARTIALLY_FILLED"} if status == "OPEN" else {status})
                    for order in strategy.orders.values())
            artifacts["diagnostics"] = _write(directory / (name + "-diagnostics.json"),
                   dict(evidence=BOUNDARY, metrics=diagnostics))
            tracker.append_run(identifier, identifier, **provenance,
                command=["python", "-m", "examples.research.grid_synthetic_demo", "--replay",
                         str(output / "manifest.json"), "--output", "NEW_OUTPUT_DIRECTORY"],
                status="SUCCEEDED", qualification="SYNTHETIC", portfolio_metrics=dict(metrics, **diagnostics),
                artifacts=artifacts, validation=dict(sample_hash=sample_hash,
                    evaluation=dict(context=asdict(context), schema=SCHEMA)))
            run_ids.append(identifier)
            summaries.append(dict(scenario=scenario, model=name, evidence=" | ".join(BOUNDARY),
                                  **metrics, **diagnostics))
        compare_results(results).to_csv(directory / "comparison.csv", index=False)
        _write(directory / "tracker-comparison.json", tracker.compare(run_ids))
    pd.DataFrame(summaries).to_csv(output / "summary.csv", index=False)
    return output
