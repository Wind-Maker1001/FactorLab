"""Offline acceptance orchestrator; existing model/statistical/backtest code is reused."""

import hashlib
import json
import os
from pathlib import Path
import platform
from unittest.mock import patch

import pandas as pd

from qbot.research.backtest import (
    ExecutionBacktestConfig, FastBacktestConfig, run_execution_backtest, run_fast_backtest,
)
from qbot.research.metrics.robustness import RobustnessOptions, evaluate_model_robustness
from qbot.research.reproducibility import package_versions
from qbot.research.validation import diagnostic_frame_sha256
import qbot.research.validation.walk_forward as wf

from .checks import assert_folds, assert_labels, future_probes, observe_model_calls, require, statistical_probes
from .profiling import Recorder
from .source_identity import (
    PROTOCOL, SourceIntegrityError, assert_source_unchanged, source_snapshot,
)
from .synthetic import (
    FEATURES, MODELS, TARGET, WARNING, build_dataset, generate_inputs, make_runner,
    model_options, split_options,
)


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def source_hash(root):
    """V2 canonical identity; old raw v1 digests are retained in source snapshots."""
    return source_snapshot(root)["canonical_sha256"]


def write_report(output, report, rows):
    software = [row for row in report["matrix"] if row["category"] != "数据资格"
                and row["check"] not in ("scale_run", "formal_parameter_stress")]
    report["software_acceptance"] = (
        "FAIL" if any(row["status"] == "FAIL" for row in software) else
        "NOT_RUN" if any(row["status"] == "NOT_RUN" for row in software) else "PASS")
    report["formal_experiment_readiness"] = "BLOCKED"
    save_json(output / "readiness.json", report)
    pd.DataFrame(rows).to_csv(output / "resources.csv", index=False)
    lines = ["# 四模型软件就绪诊断", "", WARNING, "",
             "本场景软件验收：%s；金融数据资格：Formal / Forward 均为 BLOCKED。" % report["software_acceptance"], "",
             "| 维度 | 检查 | 状态 | 原因 |", "|---|---|---|---|"]
    for row in report["matrix"]:
        lines.append("| {category} | {check} | {status} | {reason} |".format(**row))
    lines.extend(["", "资源数据见 resources.csv；嵌套阶段耗时包含子调用，不能相加。",
                  "sampled_peak_rss_bytes 为10ms采样的原生RSS峰值下界；process_high_watermark_bytes 为OS进程累计峰值。",
                  "events.jsonl 每阶段START/END落盘；硬终止时最后未结束的START提供失败位置。", ""])
    (output / "readiness.md").write_text("\n".join(lines), encoding="utf-8")


def set_row(report, check, status, reason):
    row = next(item for item in report["matrix"] if item["check"] == check)
    row.update(status=status, reason=str(reason).replace("|", "/").replace("\n", " ")[:600])


def run_acceptance(output, size="small", seed=42, probes=True):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be a new or empty directory to prevent stale evidence")
    if size not in ("small", "scale"):
        raise ValueError("size must be small or scale")
    symbols, sessions = (12, 300) if size == "small" else (500, 1200)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[4]
    source_start = source_snapshot(root)
    save_json(output / "source-start.json", source_start)
    report = {
        "schema_version": 2, "warning": WARNING, "synthetic": True,
        "financial_data_qualified": False, "size": size, "seed": seed,
        "configuration": {
            "symbols": symbols, "sessions": sessions, "features": list(FEATURES),
            "splitter": split_options(size), "models": {name: model_options(name) for name in MODELS},
            "top_k": 3 if size == "small" else 20, "minimum_cross_section": 8,
            "label": "t_close -> t+1_open -> t+6_open", "probes": probes,
            "warmup_sessions": 120, "regime_column": "MARKET_REGIME",
        },
        "environment": {"git_commit": source_start["git_commit"],
                        "source_sha256": source_start["canonical_sha256"],
                        "source_hash_protocol": PROTOCOL,
                        "source_snapshot_start": "source-start.json",
                        "python": platform.python_version(), "platform": platform.platform(),
                        "packages": package_versions(),
                        "threads": {key: os.environ.get(key) for key in (
                            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}},
        "matrix": [], "models": {},
    }
    checks = {
        "功能": ["dataset_labels", "fold_isolation", *["pipeline/" + model for model in MODELS],
                 "four_model_cohort", "future_causality"],
        "性能": ["resources", "scale_run", "formal_parameter_stress"],
        "可复现性": ["input_identity", "repeat_predictions"],
        "统计合同": ["historical_inference", "statistical_counterexamples", "comparison_counterexamples"],
        "数据资格": ["Formal", "Forward"],
    }
    for category, names in checks.items():
        for check in names:
            report["matrix"].append(dict(category=category, check=check, status="NOT_RUN", reason="尚未执行"))
    for check in ("Formal", "Forward"):
        set_row(report, check, "BLOCKED", "仅合成输入；未提供本实验可验收的五年PIT/HFQ/状态证据及Forward激活证据")
    set_row(report, "formal_parameter_stress", "NOT_RUN", "诊断使用小候选配置；未执行正式YAML完整候选资源验收")
    recorder = Recorder(output)
    write_report(output, report, recorder.rows)
    active_check = "dataset_labels"
    try:
        with recorder.span("input_build", symbols=symbols, sessions=sessions):
            inputs = generate_inputs(symbols, sessions, seed)
        with recorder.span("dataset_build"):
            dataset = build_dataset(inputs)
            assert_labels(inputs, dataset)
        set_row(report, "dataset_labels", "PASS", "既有14因子Dataset；独立验证t+1/t+6标签数值及未成熟尾部")
        active_check = "fold_isolation"
        with recorder.span("split_audit"):
            folds = assert_folds(dataset, inputs.research_calendar, size)
            assert_folds(dataset, inputs.research_calendar, size, purge_days=0, embargo_days=0)
        set_row(report, "fold_isolation", "PASS", "%d个Fold；purge/embargo为0时实际标签exit边界仍隔离" % len(folds))
        report["input_sha256"] = {
            name: diagnostic_frame_sha256(frame) for name, frame in {
                "bars": inputs.bars, "benchmark": inputs.benchmark, "membership": inputs.membership,
                "status": inputs.status, "listings": inputs.listings, "dataset": dataset,
                "calendar": pd.DataFrame({"date": inputs.research_calendar}),
            }.items()
        }
        identity = json.dumps({"inputs": report["input_sha256"], "configuration": report["configuration"],
                               "environment": report["environment"]}, sort_keys=True).encode()
        report["run_identity_sha256"] = hashlib.sha256(identity).hexdigest()
        set_row(report, "input_identity", "PASS", "七类输入内容、源代码、依赖、参数和线程设置均绑定SHA或版本")
        results = {}
        top_k = report["configuration"]["top_k"]
        for model in MODELS:
            active_check = "pipeline/" + model
            try:
                runner = make_runner(model, size, seed)
                real_evaluate = wf.evaluate_predictions

                def timed_metrics(*args, **kwargs):
                    with recorder.span("prediction_metrics", model, rows=len(args[0])):
                        return real_evaluate(*args, **kwargs)

                with recorder.span("walk_forward_total", model):
                    with observe_model_calls(model, dataset, folds, recorder) as calls:
                        with patch.object(wf, "evaluate_predictions", timed_metrics):
                            result = runner.run(dataset, calendar=inputs.research_calendar)
                prediction = result.predictions
                first, last = prediction.date.min(), prediction.exit_date.max()
                bars = inputs.bars.loc[inputs.bars.date.between(first, last)].merge(
                    inputs.status, on=["date", "symbol"], validate="one_to_one")
                benchmark = inputs.benchmark.loc[inputs.benchmark.date.between(first, last)]
                backtests = {}
                for stage, function, config in (
                    ("fast_backtest", run_fast_backtest, FastBacktestConfig(top_k=top_k, minimum_cross_section=8)),
                    ("execution_backtest", run_execution_backtest, ExecutionBacktestConfig(top_k=top_k, minimum_cross_section=8)),
                ):
                    with recorder.span(stage, model):
                        backtest = function(prediction[["date", "symbol", "score"]], bars, benchmark, config=config)
                        require(len(backtest.trades) > 0, "empty backtest trades")
                        trades = backtest.trades
                        execution_dates = pd.to_datetime(trades.execution_date)
                        signals = pd.to_datetime(trades.signal_date)
                        next_open = pd.Series(inputs.calendar, index=inputs.calendar).shift(-1)
                        require((execution_dates.to_numpy() == signals.map(next_open).to_numpy()).all(),
                                "trade did not execute at t+1 open")
                        backtests[stage] = {"trades": len(trades), "nav_sha256": diagnostic_frame_sha256(backtest.nav)}
                report["models"][model] = {
                    "run_identity_sha256": report["run_identity_sha256"], "folds": len(result.fold_summary),
                    "prediction_rows": len(prediction), "prediction_sha256": diagnostic_frame_sha256(prediction),
                    "model_call_counts": calls, "backtests": backtests,
                }
                model_dir = output / model
                model_dir.mkdir()
                prediction.to_parquet(model_dir / "predictions.parquet", index=False)
                result.fold_summary.to_csv(model_dir / "folds.csv", index=False)
                result.diagnostic_audit.to_csv(model_dir / "stage_audit.csv", index=False)
                # The scale run retains only OOS results, daily metrics and cohort audits.
                results[model] = replace_result_without_train_predictions(result)
                set_row(report, active_check, "PASS", "真实fit/predict实参隔离、Validation选参、Test单次预测及两级回测通过")
            except Exception as exc:
                set_row(report, active_check, "FAIL", "%s: %s" % (type(exc).__name__, exc))
            write_report(output, report, recorder.rows)
        if len(results) != len(MODELS):
            raise RuntimeError("four-model acceptance incomplete; see model failures and events.jsonl")
        active_check = "four_model_cohort"
        key_columns = ["date", "symbol", "fold", "entry_date", "exit_date", TARGET]
        cohort_columns = ["fold", "stage", "source_keys_sha256", "source_sha256", "fit_source_sha256"]
        for model in MODELS[1:]:
            canonical = lambda frame: frame[key_columns].sort_values(["date", "symbol", "fold"]).reset_index(drop=True)
            pd.testing.assert_frame_equal(canonical(results[MODELS[0]].predictions), canonical(results[model].predictions))
            pd.testing.assert_frame_equal(results[MODELS[0]].diagnostic_audit[cohort_columns], results[model].diagnostic_audit[cohort_columns])
        set_row(report, active_check, "PASS", "四模型共享日期、证券、Fold、标签身份、Train来源及运行身份")
        active_check = "historical_inference"
        daily = pd.concat([result.diagnostic_daily for result in results.values()], ignore_index=True)
        daily = daily.loc[daily.stage.eq("HISTORICAL_TEST")]
        with recorder.span("statistical_evaluation"):
            evaluation = evaluate_model_robustness(daily, inputs.research_calendar, RobustnessOptions(random_seed=seed))
            require(evaluation.summary.primary_hypothesis.sum() == 3, "primary family changed")
        evaluation.summary.to_csv(output / "inference.csv", index=False)
        save_json(output / "inference_protocol.json", evaluation.protocol)
        primary = evaluation.summary.loc[evaluation.summary.primary_hypothesis]
        report["primary_inference_status"] = primary.set_index("model_name").status.to_dict()
        set_row(report, active_check, "PASS", "调用既有配对RankIC/HAC/MBB/固定三项Holm；可用性状态单独保留，不评选模型")
        active_check = "statistical_counterexamples"
        with recorder.span("statistical_counterexamples"):
            report["statistical_probes"] = statistical_probes()
        set_row(report, active_check, "PASS", "缺日/低coverage/不足60日/常数/序列相关/溢出及固定Holm反例通过")
        if probes:
            active_check = "future_causality"
            with recorder.span("future_perturbations"):
                report["future_probes"] = future_probes(inputs, dataset, results, size, seed)
            set_row(report, active_check, "PASS", "未来行情及未成熟标签扰动后，历史14因子与四模型预测逐值不变")
            active_check = "repeat_predictions"
            with recorder.span("repeat_predictions"):
                rebuilt = build_dataset(generate_inputs(symbols, sessions, seed))
                pd.testing.assert_frame_equal(dataset, rebuilt, check_exact=True)
                for model, baseline in results.items():
                    repeated = make_runner(model, size, seed).run(rebuilt, calendar=inputs.research_calendar)
                    require(diagnostic_frame_sha256(repeated.predictions) == diagnostic_frame_sha256(baseline.predictions),
                            "%s predictions not reproducible" % model)
            set_row(report, active_check, "PASS", "相同seed重建Dataset并重新运行四模型，完整预测表SHA相同")
        set_row(report, "scale_run", "PASS" if size == "scale" else "NOT_RUN",
                "500证券×1200交易日，显式执行" if size == "scale" else "本次仅小规模；需显式--size scale")
    except Exception as exc:
        # Preserve a specific model's failure instead of overwriting a PASS in its peers.
        if next(row for row in report["matrix"] if row["check"] == active_check)["status"] != "PASS":
            set_row(report, active_check, "FAIL", "%s: %s" % (type(exc).__name__, exc))
        report["failure"] = "%s: %s" % (type(exc).__name__, exc)
        for row in report["matrix"]:
            if row["status"] == "NOT_RUN" and row["reason"] == "尚未执行":
                row["reason"] = "前置验收未完成：%s；见failure及events.jsonl" % active_check
    finally:
        recorder.close()
        try:
            source_end = source_snapshot(root)
            save_json(output / "source-end.json", source_end)
            assert_source_unchanged(source_start, source_end)
            report["source_verification"] = {
                "status": "PASS", "protocol": PROTOCOL,
                "start": "source-start.json", "end": "source-end.json",
                "limits": "Start/end observations; cannot detect transient changes reverted between snapshots",
            }
        except (SourceIntegrityError, OSError) as exc:
            set_row(report, "input_identity", "FAIL", str(exc))
            report["source_verification"] = {"status": "FAIL", "reason": str(exc)}
            save_json(output / "source-end-error.json", report["source_verification"])
        resource_ok = bool(recorder.rows) and not recorder.sampler_error and all(row["status"] == "PASS" for row in recorder.rows)
        set_row(report, "resources", "PASS" if resource_ok else "FAIL",
                "阶段耗时、原生RSS采样峰值与OS累计峰值已记录；无性能SLA" if resource_ok else "存在失败阶段或RSS采样失败；见events.jsonl")
        write_report(output, report, recorder.rows)
    # Keep the public return value identical to persisted JSON (tuple/list and
    # integer mapping keys otherwise differ after serialization).
    return json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))


def replace_result_without_train_predictions(result):
    from dataclasses import replace
    return replace(result, diagnostic_predictions=pd.DataFrame())
