"""Daily target research using existing execution, evaluation and immutable tracking."""
from dataclasses import asdict

import numpy as np
import pandas as pd

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.backtest.target import run_target_positions
from factorlab.dataset.identity import frame_hash, json_hash
from factorlab.errors import SchemaError
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
from factorlab.strategies.dual_ma import prepare_inputs
from factorlab.strategies.technical import technical_signals

SAFETY = "NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED"


def run_technical_backtest(bars, calendar, config, *, fee_schedule=None,
                           corporate_actions=None, source_kind="REAL_CANDIDATE"):
    """Low-level SOFTWARE_ONLY API, never a real-data admission route.

    source_kind labels evidence, not permission. Real consumers still require
    existing independent admission. No variable adjustment/corporate actions.
    """
    frame, research = prepare_inputs(bars, calendar, config)
    if corporate_actions is not None and len(corporate_actions):
        raise SchemaError("corporate-action execution ledger is unsupported")
    ratio = frame.adj_close / frame.raw_close
    if not np.allclose(ratio, ratio.iloc[0], rtol=1e-10, atol=1e-12):
        raise SchemaError("variable adjustment ratio requires qualified execution ledger")
    signals = technical_signals(frame, calendar, config)
    fees = fee_schedule or FeeSchedule.a_share_default()
    execution = ExecutionBacktestConfig(initial_cash=config.initial_cash, board_lot=config.board_lot)
    strategy = run_target_positions(signals, frame, research, execution, fees)
    targets = signals.copy()
    targets["target_weight"] = np.nan
    targets.loc[0, "target_weight"] = config.max_weight
    benchmark = run_target_positions(targets, frame, research, execution, fees, buy_at_first_open=True)
    dataset_id = frame_hash(frame)
    context = EvaluationContext(dataset_id=dataset_id, universe_id=config.symbol,
        label="daily_equity_return", label_id="raw_close_equity_v1",
        benchmark_id=json_hash({"mandate": "buy_hold_first_open", "max_weight": config.max_weight,
                                "fees": _fee_identity(fees)}),
        calendar=tuple(research.strftime("%Y-%m-%d")), holding_days=1, source_kind=source_kind)
    evaluator = ResearchEvaluation(context)
    evaluations = {}
    for name, leg in (("strategy", strategy), ("benchmark", benchmark)):
        nav = leg["nav"].copy()
        nav["benchmark_nav"] = benchmark["nav"].strategy_nav.to_numpy()
        nav["benchmark_return"] = benchmark["nav"].strategy_return.to_numpy()
        trades = leg["trades"]
        costs = trades.groupby("execution_date").transaction_cost.sum()
        notionals = trades.groupby("execution_date").trade_notional.sum()
        nav["transaction_cost"] = nav.date.map(costs).fillna(0.0)
        # One-way traded notional / previous close equity (initial capital on day 1).
        prior = nav.portfolio_value.shift(1, fill_value=config.initial_cash)
        nav["turnover"] = nav.date.map(notionals).fillna(0.0) / prior
        nav["holdings"] = nav.shares.gt(0).astype(int)
        leg["nav"] = nav
        evaluations[name] = evaluator.portfolio(name, nav, trades=trades, benchmark_only=name == "benchmark")
    return dict(signals=signals, strategy=strategy, benchmark=benchmark, evaluations=evaluations,
                dataset_id=dataset_id, safety=SAFETY, scope="SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE",
                financial_admission="FORMAL_ADMISSION_BLOCKED", pit_status="PIT_UNVERIFIED",
                hfq_status="HFQ_UNVERIFIED", no_live_money=True)


def _fee_identity(fees):
    return [{key: str(value) if key.startswith("effective_") and value is not None else value
             for key, value in asdict(rule).items()} for rule in fees.rules]


def record_technical_run(tracker, experiment_id, run_id, result, config, fees, *,
                         provenance, command, artifacts):
    """Append existing tracker records; qualification follows the evaluation input."""
    tracker.create_experiment(experiment_id, dict(model=type(config).__name__, parameters=asdict(config),
        factors=[], dataset={"bars_hash": result["dataset_id"]},
        label={"name": "daily_equity_return"},
        windows={"train": None, "validation": None, "test": [config.start, config.end]},
        walk_forward={"mode": "fixed_predeclared_parameters_no_fit"}, costs={"rules": _fee_identity(fees)}))
    metrics = {name + ":" + key: value.value
               for name, evaluation in result["evaluations"].items()
               for key, value in evaluation.metrics.items()}
    return tracker.append_run(experiment_id, run_id, **provenance, command=command,
        status="SUCCEEDED", qualification=result["evaluations"]["strategy"].context.source_kind,
        portfolio_metrics=metrics, artifacts=artifacts,
        validation={"safety": SAFETY, "sample_hash": result["dataset_id"],
                    "evaluation": result["evaluations"]["strategy"].context.__dict__})
