"""Fixed-parameter retrospective raw-price comparison; never financial admission."""
import argparse
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

from factorlab.backtest.execution import ExecutionBacktestConfig
from factorlab.backtest.fees import FeeSchedule
from factorlab.backtest.target import run_target_positions
from factorlab.data.evidence import sha256
from factorlab.errors import FactorLabError, SchemaError
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation
from factorlab.experiments.tracker import ExperimentTracker
from factorlab.metrics.risk import drawdown_series
from factorlab.strategies.dual_ma import DualMAConfig, moving_average_signals, prepare_inputs
from factorlab.strategies.dual_ma_cli import _json
from factorlab.strategies.exploratory_real import LIMITATIONS, SCOPE, load_csmar
from factorlab.strategies.technical import (
    BollingerConfig, RSIConfig, MACDConfig, CTAConfig, technical_signals,
)
from factorlab.strategies.technical_research import SAFETY, _fee_identity

CONFIG_TYPES = {"dual_ma": DualMAConfig, "bollinger": BollingerConfig,
                "rsi": RSIConfig, "macd": MACDConfig, "cta": CTAConfig}
NAMES = (*CONFIG_TYPES, "buy_hold")
RETURN_BASIS = "RAW_PRICE_RETURN_NET_OF_TRADING_COSTS"
COMPLETED = "P5_R02_EXPLORATORY_REAL_COMPARISON_COMPLETED"


def compare(bars, calendar, configuration, *, source_kind="REAL_CANDIDATE"):
    """Use one raw input, execution contract and benchmark for all six mandates."""
    if configuration["signal_price_column"] != "raw_close":
        raise SchemaError("real comparison requires explicit raw_close signals")
    if set(configuration["strategies"]) != set(NAMES):
        raise SchemaError("comparison requires exactly the six fixed mandates")
    if configuration["strategies"]["buy_hold"]:
        raise SchemaError("buy_hold uses the shared mandate without strategy overrides")
    common = configuration["common"]
    configs = {}
    for name, cls in CONFIG_TYPES.items():
        parameters = configuration["strategies"][name]
        if set(common) & set(parameters):
            raise SchemaError("strategy parameters cannot override shared execution/window")
        configs[name] = cls(**common, **parameters)
    warmup = pd.Timestamp(configuration["warmup_start"])
    if warmup >= pd.Timestamp(common["start"]):
        raise SchemaError("warmup_start must precede research start")
    dates = pd.DatetimeIndex(calendar)
    window = (dates >= warmup) & (dates <= pd.Timestamp(common["end"]))
    dates = dates[window]
    # Drop vendor adjustment fields, rather than aliasing raw into adjusted units.
    raw = bars.loc[pd.to_datetime(bars.date).between(warmup, pd.Timestamp(common["end"])),
                   [c for c in bars.columns if not c.startswith("adj_")]].reset_index(drop=True)
    frame, research = prepare_inputs(raw, dates, configs["dual_ma"], signal_price_column="raw_close")
    signals = {}
    for name, config in configs.items():
        generator = moving_average_signals if name == "dual_ma" else technical_signals
        signals[name] = generator(frame, dates, config, signal_price_column="raw_close")
    mandate = frame.loc[frame.date.isin(research), ["date", "symbol"]].reset_index(drop=True)
    mandate["signal"] = "hold"
    mandate["target_weight"] = np.nan
    mandate.loc[0, ["signal", "target_weight"]] = ["predeclared_first_open", common["max_weight"]]
    signals["buy_hold"] = mandate
    fees = FeeSchedule.a_share_default(**configuration["fees"])
    execution = ExecutionBacktestConfig(initial_cash=common["initial_cash"], board_lot=common["board_lot"])
    legs = {name: run_target_positions(signals[name], frame, research, execution, fees,
                                      buy_at_first_open=name == "buy_hold") for name in NAMES}
    context = EvaluationContext(dataset_id=configuration["manifest_sha256"],
        universe_id=common["symbol"], label="raw_price_return", label_id="raw_close_no_distributions",
        benchmark_id="buy_hold_same_execution", calendar=tuple(research.strftime("%Y-%m-%d")),
        holding_days=1, source_kind=source_kind)
    evaluator = ResearchEvaluation(context)
    evaluations, summary = {}, {}
    benchmark = legs["buy_hold"]["nav"]
    for name, leg in legs.items():
        nav, trades = leg["nav"], leg["trades"]
        nav["benchmark_nav"] = benchmark.strategy_nav.to_numpy()
        nav["benchmark_return"] = benchmark.strategy_return.to_numpy()
        nav["transaction_cost"] = nav.date.map(trades.groupby("execution_date").transaction_cost.sum()).fillna(0.0)
        notional = trades.groupby("execution_date").trade_notional.sum()
        nav["turnover"] = nav.date.map(notional).fillna(0.0) / nav.portfolio_value.shift(1, fill_value=common["initial_cash"])
        nav["holdings"] = nav.shares.gt(0).astype(int)
        nav["drawdown"] = drawdown_series(nav.strategy_nav)
        evaluation = evaluator.portfolio(name, nav, trades=trades, benchmark_only=name == "buy_hold")
        evaluations[name] = evaluation
        stats = {key: metric.value for key, metric in evaluation.metrics.items()}
        stats.update(status="SUCCEEDED", return_basis=RETURN_BASIS,
            total_return_status="TOTAL_RETURN_UNAVAILABLE", total_return=None,
            rejection_count=len(leg["rejections"]),
            rejection_reasons={str(k): int(v) for k, v in leg["rejections"].reason.value_counts().items()},
            holding_time_ratio=float(nav.holdings.mean()),
            fees_excluding_slippage=float(trades.transaction_cost.sum() - trades.slippage.sum()),
            slippage=float(trades.slippage.sum()), final_equity=float(nav.portfolio_value.iloc[-1]),
            relative_buy_hold_return=float(nav.strategy_nav.iloc[-1] / benchmark.strategy_nav.iloc[-1] - 1.0),
            terminal_pending_target=None if pd.isna(nav.pending_target.iloc[-1]) else float(nav.pending_target.iloc[-1]),
            terminal_close_target=None if pd.isna(signals[name].target_weight.iloc[-1]) else float(signals[name].target_weight.iloc[-1]))
        summary[name] = stats
    return dict(signals=signals, legs=legs, evaluations=evaluations, summary=summary,
                input_bars=frame, calendar=dates, research=research, fees=fees,
                parameters={name: asdict(config) for name, config in configs.items()},
                status=COMPLETED, safety=SAFETY)


def plot_comparison(result, output):
    """Static light-mode comparison with CSV table views and redundant line styles."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import PercentFormatter

    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
    styles = ["-", "--", "-.", ":", (0, (5, 1, 1, 1)), (0, (8, 3))]
    labels = ["MA5/20", "Bollinger", "RSI", "MACD", "Donchian/ATR", "Buy & hold"]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, layout="constrained")
    fig.set_facecolor("#fcfcfb")
    for ax in axes:
        ax.set_facecolor("#fcfcfb")
        ax.grid(axis="y", color="#e1e0d9", linewidth=0.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["bottom", "left"]].set_color("#c3c2b7")
        ax.tick_params(colors="#52514e")
    for name, color, style, label in zip(NAMES, colors, styles, labels):
        nav = result["legs"][name]["nav"]
        # Pre-open origin is explicit; no leg is rebased at its first close.
        dates = [nav.date.iloc[0] - pd.Timedelta(hours=12), *nav.date]
        axes[0].plot(dates, [1.0, *nav.strategy_nav], color=color, linestyle=style, linewidth=1.5, label=label)
        axes[1].plot(dates, [0.0, *nav.drawdown], color=color, linestyle=style, linewidth=1.5, label=label)
    axes[0].set_title("601857.SH | 2018 | Raw-price return net of trading costs", loc="left")
    axes[0].set_ylabel("NAV (initial capital = 1)")
    axes[0].legend(ncol=3, frameon=False, loc="lower left", fontsize=9)
    axes[1].set_ylabel("Drawdown from initial NAV / peak")
    axes[1].yaxis.set_major_formatter(PercentFormatter(1.0))
    axes[1].xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes[1].set_xlabel("Observed security sessions | NO LIVE MONEY | TOTAL_RETURN_UNAVAILABLE")
    fig.savefig(output / "comparison.png", dpi=160, metadata={"Software": "FactorLab"})
    plt.close(fig)


def run(config_path, source_root, output):
    configuration = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if configuration["source"] != "CSMAR_PREPARED":
        raise SchemaError("source must explicitly select CSMAR_PREPARED")
    output = Path(output)
    try:
        bars, calendar, source, events = load_csmar(source_root, configuration["manifest_sha256"])
    except FileNotFoundError as exc:
        output.mkdir(parents=True, exist_ok=False)
        _json(output / "config.json", configuration)
        _json(output / "manifest.json", dict(status="DATA_UNAVAILABLE", missing=str(exc.filename),
            source_manifest_sha256=configuration["manifest_sha256"], safety=SAFETY,
            strategies={name: {"status": "DATA_UNAVAILABLE"} for name in NAMES},
            reproduction=[sys.executable, "-I", "tools/research/run_real_strategy_comparison.py",
                "--config", str(config_path), "--source-root", str(source_root), "--output", "<NEW_PRIVATE_OUTPUT>"]))
        return {"status": "DATA_UNAVAILABLE"}, {}
    result = compare(bars, calendar, configuration)
    output.mkdir(parents=True, exist_ok=False)
    _json(output / "config.json", configuration)
    result["input_bars"].to_csv(output / "input_bars.csv", index=False, float_format="%.17g")
    pd.DataFrame({"date": result["calendar"]}).to_csv(output / "calendar.csv", index=False)
    for name in NAMES:
        result["signals"][name].to_csv(output / (name + "_signals.csv"), index=False, float_format="%.17g")
        for key in ("nav", "trades", "rejections"):
            result["legs"][name][key].to_csv(output / (name + "_" + key + ".csv"), index=False, float_format="%.17g")
        _json(output / (name + "_evaluation.json"), result["evaluations"][name].to_dict())
    _json(output / "metrics.json", result["summary"])
    pd.DataFrame(result["summary"]).T.drop(columns="rejection_reasons").to_csv(output / "metrics.csv", float_format="%.17g")
    plot_comparison(result, output)
    repo = Path(__file__).resolve().parents[3]
    git = lambda *args: subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    environment = {"python": sys.version, "platform": platform.platform(),
        "packages": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "pyarrow", "matplotlib")}}
    command = [sys.executable, "-I", "-B", "-X", "utf8", str(repo / "tools/research/run_real_strategy_comparison.py"),
        "--config", str((output / "config.json").resolve()), "--source-root", str(Path(source_root).resolve()),
        "--output", "<NEW_PRIVATE_OUTPUT>"]
    code = dict(commit=git("rev-parse", "HEAD"), tree=git("rev-parse", "HEAD^{tree}"),
                dirty=bool(git("status", "--porcelain")))
    manifest = dict(schema="FACTORLAB_REAL_COMPARISON_V1", status=COMPLETED, scope=SCOPE,
        safety=SAFETY, no_live_money=True, pit_status="PIT_UNVERIFIED", hfq_status="HFQ_UNVERIFIED",
        formal_admission="FORMAL_ADMISSION_BLOCKED", execution_admission=False,
        forward_admission=False, return_basis=RETURN_BASIS,
        total_return_status="TOTAL_RETURN_UNAVAILABLE", total_return=None,
        source_manifest_sha256=configuration["manifest_sha256"], source=source,
        known_action_candidates=events, limitations=LIMITATIONS,
        rows=len(result["input_bars"]), warmup_rows=int((result["calendar"] < result["research"][0]).sum()),
        research_rows=len(result["research"]), first_observed=str(result["calendar"][0].date()),
        first_research=str(result["research"][0].date()), last_observed=str(result["calendar"][-1].date()),
        parameters=result["parameters"], fees=_fee_identity(result["fees"]), code=code,
        source_sha256={p.relative_to(repo).as_posix(): sha256(p.read_bytes())
            for p in sorted((repo / "src/factorlab").rglob("*.py"))},
        entrypoint_sha256=sha256((repo / "tools/research/run_real_strategy_comparison.py").read_bytes()),
        environment=environment, reproduction=command,
        artifacts_sha256={p.name: sha256(p.read_bytes()) for p in sorted(output.iterdir())})
    _json(output / "manifest.json", manifest)
    tracker = ExperimentTracker(output / "tracker")
    tracker.create_experiment("p5-r02", dict(model="six_fixed_raw_price_mandates",
        parameters={"common": configuration["common"], "strategies": configuration["strategies"]}, factors=[],
        dataset={"source": configuration["source"], "manifest_sha256": configuration["manifest_sha256"]},
        label={"return_basis": RETURN_BASIS}, windows={"train": None, "validation": None,
            "test": [configuration["common"]["start"], configuration["common"]["end"]]},
        walk_forward={"mode": "fixed_predeclared_parameters_no_fit"}, costs=configuration["fees"]))
    tracker.append_run("p5-r02", "comparison", code=code, environment=environment, command=command,
        status="SUCCEEDED", qualification="REAL_CANDIDATE",
        portfolio_metrics={name + ":" + key: metric.value for name, evaluation in result["evaluations"].items()
                           for key, metric in evaluation.metrics.items()},
        artifacts={"manifest": {"path": str((output / "manifest.json").resolve())}},
        validation={"scope": SCOPE, "safety": SAFETY, "total_return": "TOTAL_RETURN_UNAVAILABLE"})
    return manifest, result["summary"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="EXPLORATORY_REAL_DATA: six raw-price mandates; NO LIVE MONEY")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest, summary = run(args.config, args.source_root, args.output)
        print(json.dumps({"status": manifest["status"], "metrics": summary}, ensure_ascii=False, allow_nan=False))
        return 0 if manifest["status"] == COMPLETED else 2
    except (FactorLabError, ValueError, KeyError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
