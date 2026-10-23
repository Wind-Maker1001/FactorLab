"""Chinese exploratory disclosure, derived tables, and marked PNG figures."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from factorlab.reports.generator import write_factor_report

from .contract import ExploratoryContractError, REQUIRED_MARKERS


def _save(figure, destination, software_test_only=False):
    markers = list(REQUIRED_MARKERS)
    if software_test_only:
        markers.insert(0, "SYNTHETIC_TEST_ONLY / SOFTWARE RENDER CHECK")
    figure.text(
        0.5, 0.015, " | ".join(markers), ha="center", fontsize=6.6, color="#85351b"
    )
    figure.tight_layout(rect=(0, 0.045, 1, 1))
    figure.savefig(destination, dpi=160, metadata={"Description": " | ".join(markers)})
    plt.close(figure)


def _table(frame, columns=None):
    """No optional tabulate dependency; preserve missing values as N/A."""
    data = frame if columns is None else frame.loc[:, columns]
    lines = [
        "| " + " | ".join(map(str, data.columns)) + " |",
        "| " + " | ".join("---" for _ in data.columns) + " |",
    ]
    for values in data.itertuples(index=False, name=None):
        cells = []
        for value in values:
            if pd.isna(value):
                cells.append("N/A")
            elif isinstance(value, (float, np.floating)):
                cells.append("%.6f" % value)
            else:
                cells.append(str(value).replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _bar(values, title, label, path, zero=True, software_test_only=False):
    figure, axis = plt.subplots(figsize=(12, 5))
    values.plot(kind="bar", ax=axis, color="#366b91")
    if zero:
        axis.axhline(0, color="#333333", linewidth=0.8)
    axis.set_title(title)
    axis.set_ylabel(label)
    _save(figure, path, software_test_only=software_test_only)


def _plots(destination, analysis):
    ev = analysis.evaluation
    software_test_only = analysis.quality.get("synthetic_only") is True

    def save(figure, path):
        _save(figure, path, software_test_only=software_test_only)

    summary = ev.summary.loc[ev.summary["scope"] == "ALL"].set_index("factor")
    _bar(
        summary["IC Mean"],
        "Mean daily Pearson IC",
        "Pearson IC",
        destination / "factor_ic.png",
        software_test_only=software_test_only,
    )
    _bar(
        summary["RankIC Mean"],
        "Mean daily Spearman RankIC",
        "RankIC",
        destination / "rank_ic.png",
        software_test_only=software_test_only,
    )
    matrix = ev.correlation
    figure, axis = plt.subplots(figsize=(11, 9))
    picture = axis.imshow(
        matrix.to_numpy(dtype=float), vmin=-1, vmax=1, cmap="coolwarm"
    )
    axis.set_xticks(np.arange(len(matrix.columns)))
    axis.set_xticklabels(matrix.columns, rotation=90)
    axis.set_yticks(np.arange(len(matrix.index)))
    axis.set_yticklabels(matrix.index)
    axis.set_title("Mean daily cross-sectional Pearson factor correlation")
    figure.colorbar(picture, ax=axis)
    save(figure, destination / "factor_correlation.png")
    figure, axis = plt.subplots(figsize=(14, 6))
    q = ev.quantile_returns.loc[ev.quantile_returns["scope"] == "ALL"]
    means = q.groupby(["factor", "quantile"])["future_return"].mean().unstack()
    if not means.empty:
        means.reindex(summary.index).reindex(
            columns=["Q1", "Q2", "Q3", "Q4", "Q5", "Q5-Q1"]
        ).plot(kind="bar", ax=axis)
    axis.axhline(0, color="#333333", linewidth=0.8)
    axis.set_title(
        "Observed-label-only quintile mean forward excess return (t+1 to t+6)"
    )
    axis.set_ylabel("5-session excess return; daily overlapping horizons")
    save(figure, destination / "quantile_returns.png")
    states = ev.summary.loc[ev.summary["scope"] != "ALL"].pivot(
        index="factor", columns="scope", values="RankIC Mean"
    )
    figure, axis = plt.subplots(figsize=(14, 6))
    states.reindex(summary.index).reindex(columns=["BULL", "BEAR", "SIDEWAYS"]).plot(
        kind="bar", ax=axis
    )
    axis.axhline(0, color="#333333", linewidth=0.8)
    axis.set_title("CSI500 frozen market-regime RankIC (missing states remain N/A)")
    axis.set_ylabel("Mean daily Spearman RankIC")
    save(figure, destination / "regime_performance.png")
    figure, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    coverage = analysis.daily_coverage.set_index("date")
    coverage[["available_adj_close", "available_turnover", "available_labels"]].plot(
        ax=axes[0]
    )
    axes[0].axhline(20, color="#aa3b30", linestyle="--", label="minimum=20")
    axes[0].legend(loc="lower left", ncol=4)
    axes[0].set_title(
        "Daily available cross section (fixed pre-window eligible sample)"
    )
    axes[0].set_ylabel("Securities")
    factor_pairs = coverage.loc[
        :, [name for name in coverage.columns if name.endswith("_labeled_available")]
    ]
    pd.DataFrame(
        {
            "minimum across 14 factors": factor_pairs.min(axis=1),
            "maximum across 14 factors": factor_pairs.max(axis=1),
        },
        index=coverage.index,
    ).plot(ax=axes[1])
    axes[1].axhline(20, color="#aa3b30", linestyle="--")
    axes[1].set_ylabel("Valid factor/label pairs")
    axes[1].legend()
    save(figure, destination / "daily_coverage.png")
    daily = ev.daily_ic.loc[ev.daily_ic["scope"] == "ALL"]
    figure, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    for axis, name in zip(axes, ["IC", "RankIC"]):
        table = daily.pivot(index="date", columns="factor", values=name)
        # A rolling display only; the published daily table is untouched.
        table.rolling(20, min_periods=20).mean().plot(ax=axis, linewidth=1)
        axis.axhline(0, color="#333333", linewidth=0.8)
        axis.set_ylabel("20-session mean " + name)
        axis.legend(loc="upper left", ncol=7, fontsize=7)
    axes[0].set_title("Daily factor IC/RankIC diagnostics (20-session display mean)")
    save(figure, destination / "daily_ic.png")
    figure, axes = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    auto = (
        ev.autocorrelation.loc[ev.autocorrelation["scope"] == "ALL"]
        .groupby("factor")["autocorrelation"]
        .mean()
    )
    turn = (
        ev.turnover.loc[ev.turnover["scope"] == "ALL"]
        .groupby("factor")["top_quantile_turnover"]
        .mean()
    )
    auto.reindex(summary.index).plot(kind="bar", ax=axes[0], color="#366b91")
    turn.reindex(summary.index).plot(kind="bar", ax=axes[1], color="#987138")
    axes[0].set_title("Adjacent actual-session diagnostics; no regime-gap skipping")
    axes[0].set_ylabel("Mean rank autocorrelation")
    axes[1].set_ylabel("Mean top-quintile one-way turnover")
    save(figure, destination / "temporal_diagnostics.png")
    quality = analysis.factor_quality.set_index("factor")
    _bar(
        quality[["raw_completeness", "processed_completeness"]],
        "14-factor completeness",
        "Fraction",
        destination / "factor_completeness.png",
        zero=False,
        software_test_only=software_test_only,
    )


def write_exploratory_report(destination, manifest, analysis):
    if manifest.get("synthetic_only") is not False:
        raise ExploratoryContractError(
            "SYNTHETIC_TEST_ONLY cannot write a real empirical report"
        )
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    # Reuse the existing table/export contract, then provide the explicitly
    # isolated disclosures and marked charts in this directory only.
    write_factor_report(analysis.evaluation, destination)
    analysis.daily_coverage.to_csv(destination / "daily_coverage.csv", index=False)
    analysis.factor_quality.to_csv(destination / "factor_quality.csv", index=False)
    analysis.regime_counts.to_csv(destination / "regime_counts.csv", index=False)
    analysis.security_coverage.to_csv(
        destination / "security_coverage.csv", index=False
    )
    analysis.frame[
        [
            "date",
            "symbol",
            "entry_date",
            "exit_date",
            "STOCK_FWD_RET5",
            "BENCHMARK_FWD_RET5",
            "EXCESS_RET5",
            "MARKET_REGIME",
        ]
        + list(analysis.factor_quality["factor"])
    ].to_parquet(destination / "derived_factor_labels.parquet", index=False)
    (destination / "data_quality.json").write_text(
        json.dumps(analysis.quality, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    public_manifest = {
        key: value for key, value in manifest.items() if key not in {"sample"}
    }
    (destination / "input_manifest.json").write_text(
        json.dumps(public_manifest, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    (destination / "frozen_sample.json").write_text(
        json.dumps(manifest["sample"], ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    _plots(destination, analysis)
    q = analysis.quality
    sample = manifest["sample"]
    ev = analysis.evaluation
    all_summary = ev.summary.loc[ev.summary["scope"] == "ALL"]
    quantiles = (
        ev.quantile_returns.loc[ev.quantile_returns["scope"] == "ALL"]
        .groupby(["factor", "quantile"])["future_return"]
        .mean()
        .unstack()
        .reset_index()
    )
    auto = (
        ev.autocorrelation.loc[ev.autocorrelation["scope"] == "ALL"]
        .groupby("factor")["autocorrelation"]
        .mean()
    )
    turn = (
        ev.turnover.loc[ev.turnover["scope"] == "ALL"]
        .groupby("factor")["top_quantile_turnover"]
        .mean()
    )
    diagnostics = pd.DataFrame(
        {
            "factor": all_summary["factor"].to_numpy(),
            "mean_adjacent_rank_autocorrelation": all_summary["factor"]
            .map(auto)
            .to_numpy(),
            "mean_top_quintile_turnover": all_summary["factor"].map(turn).to_numpy(),
        }
    )
    weak_states = analysis.regime_counts.loc[
        analysis.regime_counts["sample_insufficient"], "scope"
    ].tolist()
    lines = [
        "# 真实 A 股 14 因子探索性评价报告",
        "",
        "**SUCCESS · " + " · ".join(REQUIRED_MARKERS) + "**",
        "",
        "本次成功表示真实供应商输入完成数据检查、14 因子和描述性评价闭环，与因子方向或盈利无关。数据资格仍为 UNVERIFIED；formal_admission=False，禁止用于 Forward、正式证据、模型部署和实盘输入。",
        "",
        "## 数据来源与版本",
        "",
        "- 单一行情供应商：`%s`；源版本：`%s`。"
        % (manifest["vendor"], manifest["source_version"]),
        "- 数据版本（语义 SHA256）：`%s`。" % manifest["data_version"],
        "- 获取时间：`%s`；接口、源文件、覆盖范围与逐文件 SHA256 见 [input_manifest.json](input_manifest.json)。"
        % manifest["retrieved_at"],
        "- 证券 raw_* 是未复权人民币 OHLC，adj_* 是同一供应商现成 HFQ OHLC。volume 是原始股数，amount 是原始人民币元，turnover 是原始比例。未将不同供应商 HFQ 拼接；未独立复刻或认证供应商后复权算法。",
        "- 基准为单独声明的 `CSI500_PRICE_INDEX_UNADJUSTED` 中证500价格指数，单位是指数点；其 adj_* 列是同一指数开收盘的接口兼容别名，不是股票 HFQ，不能声称基准另有供应商后复权算法。",
        "- 真实原始行情仅存私有 data/research/exploratory；本目录保留派生表、哈希、来源声明，不发布原始供应商行情。",
        "",
        "## 固定样本与实际覆盖",
        "",
        "- 选择方法：%s。选择日期：`%s`，冻结时间：`%s`。"
        % (sample["selection_method"], sample["selection_date"], sample["frozen_at"]),
        "- `NON_PIT_CONVENIENCE`，pit=False。冻结 %d 只，实际取得 %d 只，分析 %d 只；%d 只因窗口前完整预热不足被排除。未按未来收益、未来状态或研究期表现换股。"
        % (
            q["frozen_security_count"],
            q["acquired_security_count"],
            q["analyzed_security_count"],
            q["excluded_by_pre_window_count"],
        ),
        "- 研究信号窗口 %s 至 %s；实际 %s 至 %s，%d 个交易日。全日历 %s 至 %s，窗口前 %d 个实际交易日，分析证券最少 %d 个真实完整预热日，窗口后 %d 日。"
        % (
            q["requested_start"],
            q["requested_end"],
            q["actual_signal_start"],
            q["actual_signal_end"],
            q["signal_sessions"],
            q["full_calendar_start"],
            q["full_calendar_end"],
            q["calendar_warmup_sessions"],
            q["minimum_security_complete_warmup"],
            q["future_calendar_sessions"],
        ),
        "- 预热资格只用信号窗口前 OHLC、量额、换手数据决定。因子滚动先重建真实交易日×固定证券面板；缺行情保持 NaN，不填前值、不后填、不把停牌间隔当成连续观察。逐证券覆盖见 [security_coverage.csv](security_coverage.csv)。",
        "",
        "## 数据质量",
        "",
        "重复键 %d；研究预期 %d 行；标签缺失 %d 行（%.4f%%）；零成交量 %d 行；换手率超过 100%% 的 %d 行；全部历史中后复权收盘绝对日收益超过 30%% 的 %d 行，作为异常诊断保留、不静默裁剪。"
        % (
            q["duplicate_stock_keys"],
            q["expected_signal_rows"],
            q["label_missing_rows"],
            q["label_missing_rate"] * 100,
            q["zero_volume_rows"],
            q["turnover_above_100_percent_rows"],
            q["adj_close_absolute_daily_return_above_30_percent_rows"],
        ),
        "",
        _table(
            pd.DataFrame(
                {
                    "field": list(q["field_missing_rates"]),
                    "missing_rate": list(q["field_missing_rates"].values()),
                }
            )
        ),
        "",
        "中证500研究期字段缺失率：",
        "",
        _table(
            pd.DataFrame(
                {
                    "benchmark_field": list(q["benchmark_research_missing_rates"]),
                    "missing_rate": list(
                        q["benchmark_research_missing_rates"].values()
                    ),
                }
            )
        ),
        "",
        "市场状态60日历史特征不足的研究日为 %d；冻结规则会把不满足牛/熊条件的日期判为 SIDEWAYS，缺失历史特征仍保留缺失，不能把这类日子解读为已充分确认的震荡行情。"
        % q["market_regime_incomplete_trailing_feature_rows"],
        "",
        "OHLC 正值、区间顺序、RAW/HFQ 字段关系、股数整数、成交额/股数 VWAP 与 raw_low/raw_high、换手单位、日期日历、来源版本、文件哈希均由独立 contract 检查；这些检查不签发正式金融资格。更多诊断见 [data_quality.json](data_quality.json)。",
        "",
        "每日可用行情/换手/标签及每因子横截面样本数见 [daily_coverage.csv](daily_coverage.csv)；最低评价横截面固定 20，未为跑通降低。",
        "",
        "![每日横截面](daily_coverage.png)",
        "",
        "## 冻结研究合同与评价适用性",
        "",
        "t 日收盘后计算；t+1 实际交易日后复权开盘进入；t+6 实际交易日后复权开盘退出。EXCESS_RET5 = 股票五交易日收益 − 中证500同期开盘收益。未来所需开盘缺失时标签保持缺失，包括末尾未成熟标签。因子与市场状态均只使用 t 及此前数据。",
        "14 因子复用 registry，MAD×5（1.4826 尺度）后以当日横截面 ddof=0 Z-score 标准化；预处理只用当日因子，不依赖未来标签。IC、RankIC、五分位均调用现有 evaluate_factors，未修改正式数学或 Golden。",
        "**探索分位适配：仅在事后可观测 EXCESS_RET5 的评价行上调用现有五分位统计，缺失标签不参与边界。** 这是用户要求的描述性回顾评价，与正式 Golden 中所有当期有效因子决定边界的适用口径不同。缺失未来标签的筛除仅作用于该评价，不反向修改股票池、因子、当期信号、自相关、换手或相关矩阵；该分位结果不能直接解释为可交易组合收益。",
        "五分位按因子从低至高为 Q1…Q5，原始方向不按结果翻转。未来收益为超额收益的日均值；Q5−Q1 不是成本后的策略收益，重叠五日标签不是独立每日样本。ICIR / RankICIR 为均值÷日度标准差，未年化。",
        "",
        "## 14 因子完整率与异常诊断",
        "",
        _table(
            analysis.factor_quality,
            [
                "factor",
                "raw_completeness",
                "processed_completeness",
                "missing_raw_observations",
                "infinite_raw_observations",
                "zero_raw_observations",
                "mad_outliers_before_preprocessing",
                "raw_min",
                "raw_p01",
                "raw_p99",
                "raw_max",
            ],
        ),
        "",
        "MAD 异常计数只作诊断；MAD=0 的截面不伪造异常阈值。详细统计见 [factor_quality.csv](factor_quality.csv)。",
        "",
        "![完整率](factor_completeness.png)",
        "",
        "## 每日 IC / RankIC 与 ALL 汇总",
        "",
        _table(
            all_summary,
            [
                "factor",
                "IC Mean",
                "IC Std",
                "IC > 0 Ratio",
                "RankIC Mean",
                "RankIC Std",
                "RankIC > 0 Ratio",
                "Valid Dates",
                "RankIC Valid Dates",
                "Coverage",
            ],
        ),
        "",
        "每日 Pearson IC、Spearman RankIC、观测数与覆盖率见 [daily_ic.csv](daily_ic.csv)；缺失、常数或横截面不足保持 N/A。以下时间图为 20 个实际交易日滚动显示，不替换原始每日结果。",
        "",
        "![RankIC均值](rank_ic.png)",
        "",
        "![每日IC诊断](daily_ic.png)",
        "",
        "## 五分位未来超额收益与 Q5−Q1",
        "",
        _table(quantiles),
        "",
        "每日各分位观察数、已标记观察数和标签覆盖率见 [quantile_returns.csv](quantile_returns.csv)。",
        "",
        "![分位收益](quantile_returns.png)",
        "",
        "## 市场状态分组",
        "",
        "复用冻结中证500规则：close>MA60 且 MA20>MA60 为 BULL；close<MA60 且 MA20<MA60 为 BEAR；其余 SIDEWAYS。未根据本次评价调整规则。",
        "",
        _table(analysis.regime_counts),
        "",
        "少于 60 个信号日的状态仅能用于描述，不能支持稳定性结论：%s。即使信号日数达到 60，也应同时检查逐因子有效日期和观察数；重叠标签仍降低独立信息量。"
        % (", ".join(weak_states) or "无"),
        "",
        _table(
            ev.summary,
            [
                "scope",
                "factor",
                "RankIC Mean",
                "RankIC Std",
                "RankIC > 0 Ratio",
                "RankIC Valid Dates",
                "Coverage",
            ],
        ),
        "",
        "![状态评价](regime_performance.png)",
        "",
        "## 相关、自相关与换手",
        "",
        "相关矩阵为每日横截面 Pearson 相关的等权均值，见 [factor_correlation.csv](factor_correlation.csv)。自相关是相邻实际交易日共有证券的 Spearman 相关；换手为最高五分位等权篮子的单边权重变化 0.5×Σ|w_t−w_(t−1)|。按当前日状态归组前先在完整交易日历计算，未跳过异状态日期。",
        "",
        _table(diagnostics),
        "",
        "![因子相关矩阵](factor_correlation.png)",
        "",
        "![时序诊断](temporal_diagnostics.png)",
        "",
        "## 偏差、失败项与下一阶段",
        "",
        "1. 股票列表为事后冻结的非 PIT 便利样本，可能包含生存者偏差、当前可取得数据偏差与样本构成偏差；不代表完整历史中证500。选择日期晚于研究窗口时，该限制尤其明确。",
        "2. HISTORICALLY_RECONSTRUCTED 行情是供应商事后重建后复权值；供应商可能修订历史。获取版本和哈希能冻结本次字节，不能证明当时已可见或认证全部复权算法。",
        "3. 缺失标签限定描述性分位的评价子样本，可能造成标签可取得性选择偏差。未补造未来价格。股票停牌、涨跌停、ST、退市和不可成交风险未取得完整 PIT 状态，不能宣称已纳入可执行交易研究。",
        "4. 本次为一年窗口、%d 只便利样本；14 因子和多个状态存在多重比较，标签重叠；本轮不计算可执行策略利润，不训练四个模型，不依据结果调因子或划分状态。"
        % q["analyzed_security_count"],
        "5. 市场状态不足、字段缺失、预热排除和异常行均如上披露；即使 SUCCESS 也没有生成正式资格收据、Forward 输入或实盘许可。",
        "",
        "**下一阶段：可扩展真实数据采集与隔离探索验证；目前不适合宣称五年四模型正式研究已具备准入。** 应先补齐五年同源行情、历史 PIT 股票池/状态、公司行动与供应商复权可审计证据、独立标签尾部覆盖和足够状态样本，保持正式门禁，再按冻结训练/验证/测试合同评估四模型。正 RankIC 不是扩展准入条件。",
        "",
        "## 可复现收据",
        "",
        "[receipt.json](receipt.json) 绑定数据语义版本、供应商版本、源/产物哈希、代码哈希和 Python/NumPy/Pandas 环境。入口 `python -m factorlab.exploratory run --manifest <private manifest> --output results/exploratory`；私有运行复核 `python -m factorlab.exploratory verify --receipt <receipt.json>`；公开派生包复核 `python -m factorlab.exploratory.publication --verify <public receipt.json>`。外部复现需要获得同一授权私有数据；仓库中的派生报告不能替代原始输入授权。",
        "",
        "**" + " · ".join(REQUIRED_MARKERS) + "**",
        "",
    ]
    (destination / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def write_blocked_report(destination, manifest, reasons, prescreen=None):
    from .runner import FACTOR_NAMES

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(
        {
            "factor": FACTOR_NAMES,
            "IC Mean": np.nan,
            "RankIC Mean": np.nan,
            "Q5-Q1": np.nan,
            "valid_dates": 0,
            "result": "NOT_COMPUTED_REAL_INPUT_BLOCKED",
        }
    )
    summary.to_csv(destination / "factor_report.csv", index=False)
    states = pd.DataFrame(
        {
            "scope": ["ALL", "BULL", "BEAR", "SIDEWAYS"],
            "observed_real_signal_dates": np.nan,
            "valid_evaluated_dates": 0,
            "status": "UNKNOWN_REAL_INPUT_BLOCKED",
        }
    )
    if prescreen is not None:
        states = prescreen["regime_counts"]
        prescreen["daily_coverage"].to_csv(
            destination / "daily_coverage.csv", index=False
        )
        prescreen["security_coverage"].to_csv(
            destination / "security_coverage.csv", index=False
        )
        (destination / "frozen_sample.json").write_text(
            json.dumps(manifest["sample"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (destination / "input_manifest.json").write_text(
            json.dumps(
                {key: value for key, value in manifest.items() if key != "sample"},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
    states.to_csv(destination / "regime_counts.csv", index=False)
    gaps = {
        "status": "BLOCKED",
        "markers": list(REQUIRED_MARKERS),
        "reasons": reasons,
        "empirical_results_computed": False,
        "synthetic_replacement_used": False,
        "formal_admission": False,
        "forward_allowed": False,
        "live_allowed": False,
    }
    if prescreen is not None:
        gaps.update(prescreen["quality"])
    (destination / "data_quality.json").write_text(
        json.dumps(gaps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    figure, axis = plt.subplots(figsize=(12, 4))
    axis.axis("off")
    axis.text(
        0.02,
        0.88,
        "BLOCKED: NO VALID REAL EMPIRICAL RESULTS",
        fontsize=16,
        weight="bold",
        transform=axis.transAxes,
    )
    axis.text(
        0.02,
        0.58,
        "14 factors: N/A\nALL / BULL / BEAR / SIDEWAYS: not evaluated\nSee summary.md for the exact input gaps.\nNo synthetic market data substituted.",
        fontsize=12,
        transform=axis.transAxes,
    )
    _save(figure, destination / "data_gaps.png")
    lines = [
        "# 真实 A 股 14 因子探索研究：BLOCKED",
        "",
        "**BLOCKED · " + " · ".join(REQUIRED_MARKERS) + "**",
        "",
        "必要真实输入未通过隔离探索合同，未计算有效实证结果；没有以合成行情替代真实行情。正式金融门禁与旧收据保持原状，formal_admission=False，禁止用于 Forward 或实盘。",
        "",
        "## 具体缺口",
        "",
    ]
    lines.extend("- " + str(reason) for reason in reasons)
    if manifest.get("synthetic_only") is True:
        lines.extend(
            [
                "",
                "**SYNTHETIC_TEST_ONLY 输入已拒绝。** 这是软件测试夹具，不是本次真实行情来源；失败收据忠实记录 synthetic_only=True，不生成真实探索实证。",
            ]
        )
    failures = manifest.get("failures", manifest.get("excluded_securities"))
    if failures:
        lines.extend(
            [
                "",
                "供应商获取失败记录：",
                "",
                "```json",
                json.dumps(failures, ensure_ascii=False, indent=2),
                "```",
            ]
        )
    if prescreen is not None:
        q = prescreen["quality"]
        sample = manifest["sample"]
        lines.extend(
            [
                "",
                "## 真实部分输入与质量预检查",
                "",
                "真实输入已通过数据合同但尚不满足有效研究的规模/覆盖要求。供应商 `%s`，版本 `%s`，获取时间 `%s`，数据版本 `%s`。基准单独为 `CSI500_PRICE_INDEX_UNADJUSTED` 指数点；股票 raw_* 未复权人民币 OHLC、adj_* 同源供应商 HFQ。"
                % (
                    manifest["vendor"],
                    manifest["source_version"],
                    manifest["retrieved_at"],
                    manifest["data_version"],
                ),
                "",
                "冻结 %d 只，真实取得 %d 只，满足窗口前 %d 完整交易日预热的 %d 只；实际有效评价 0 只，最低横截面仍为 20。真实信号日期 %s 至 %s，共 %d 日；全日历 %s 至 %s，窗口前 %d 日，已取得证券最少 %d 完整预热日。"
                % (
                    q["frozen_security_count"],
                    q["acquired_security_count"],
                    manifest["warmup_min_sessions"],
                    q["warmup_eligible_security_count"],
                    q["actual_signal_start"],
                    q["actual_signal_end"],
                    q["signal_sessions"],
                    q["full_calendar_start"],
                    q["full_calendar_end"],
                    q["calendar_warmup_sessions"],
                    q["minimum_acquired_complete_warmup"],
                ),
                "",
                "选择方法：%s；选择日期 `%s`，冻结时间 `%s`；NON_PIT_CONVENIENCE，非历史PIT，须披露生存者偏差。未根据未来状态或收益替换样本。"
                % (
                    sample["selection_method"],
                    sample["selection_date"],
                    sample["frozen_at"],
                ),
                "",
                _table(
                    pd.DataFrame(
                        {
                            "field": list(q["field_missing_rates"]),
                            "missing_rate_acquired_panel": list(
                                q["field_missing_rates"].values()
                            ),
                        }
                    )
                ),
                "",
                "重复键 %d；零成交量 %d 行；换手超过100%% %d 行。缺失率以已取得证券×实际研究日为分母，不表示未取得的冻结证券已有行情。逐证券（含未取得者）覆盖见 [security_coverage.csv](security_coverage.csv)，每日价格/成交量/成交额/换手可用数量见 [daily_coverage.csv](daily_coverage.csv)。"
                % (
                    q["duplicate_stock_keys"],
                    q["zero_volume_rows"],
                    q["turnover_above_100_percent_rows"],
                ),
                "",
                "仅预检查真实输入，没有构造未来收益、没有输出任何因子 IC 或分位统计；未来缺失价格仍保持缺失。",
            ]
        )
    lines.extend(
        [
            "",
            "## 14 因子结果",
            "",
            _table(summary),
            "",
            "## 市场状态与样本",
            "",
            _table(states),
            "",
            "有效因子评价日期为 0。%s 没有有效分析时不能声称已计算 IC、分位、相关、自相关或换手。"
            % (
                "观测真实基准交易日与状态如上，仅来自冻结规则的基准输入预检查。"
                if prescreen is not None
                else "观测真实交易日数量未知（N/A）；这不意味着真实市场没有交易日。"
            ),
            "",
            "![数据缺口](data_gaps.png)",
            "",
            "## 偏差与下一阶段",
            "",
            "股票池预设为 NON_PIT_CONVENIENCE，pit=False；无法证明历史中证500成员资格，可能存在生存者偏差。供应商事后 HFQ 若取得仍须标记 HISTORICALLY_RECONSTRUCTED。缺失价格、换手、基准、实际日历、130交易日预热或版本/哈希任一必要输入均不能静默填补。",
            "",
            "当前不适合进入五年四模型正式研究。先补齐已列真实输入、授权来源与一致版本，完成本轮至少20只有效横截面，再评估扩大隔离探索样本；正式扩展还须补历史 PIT 与金融资格证据。",
            "",
            "[receipt.json](receipt.json) 保存失败原因、环境/代码哈希和已生成派生产物哈希。私有复核 `python -m factorlab.exploratory verify --receipt <receipt.json>`；公开派生包复核 `python -m factorlab.exploratory.publication --verify <public receipt.json>`。",
            "",
            "**" + " · ".join(REQUIRED_MARKERS) + "**",
            "",
        ]
    )
    (destination / "summary.md").write_text("\n".join(lines), encoding="utf-8")
