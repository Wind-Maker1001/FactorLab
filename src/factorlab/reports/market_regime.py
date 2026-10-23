"""Exclusive CSV/PNG/Markdown output for independent historical diagnostics."""

from pathlib import Path
from typing import Dict

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

from factorlab.diagnostics.market_regime import (
    DISCLAIMER, DIAGNOSTIC_REGIMES, MarketRegimeDiagnosis,
)


COLORS = {"BULL": "#32936f", "BEAR": "#dc6464", "SIDEWAYS": "#e5bb52", "UNKNOWN": "#a7acb5"}


def _title(synthetic: bool) -> str:
    return "SYNTHETIC SOFTWARE CHECK — NOT REAL MARKET DATA" if synthetic else "CSI500 · cached historical observations"


def _build_history_figure(diagnosis: MarketRegimeDiagnosis, synthetic: bool = False):
    figure, axis = plt.subplots(figsize=(16, 5.5))
    for _, spell in diagnosis.spells.iterrows():
        axis.axvspan(spell["start"], spell["plot_end"], color=COLORS[spell["regime"]], alpha=0.22,
                     gid="spell-%d-%s" % (spell["spell_id"], spell["regime"]))
    for column, label, color, width in (
        ("adj_close", "CSI500 adjusted close", "#203c61", 1.2),
        ("MA20", "Trailing MA20", "#007f86", 0.8),
        ("MA60", "Trailing MA60", "#7d4993", 0.8),
    ):
        axis.plot(diagnosis.daily["date"], diagnosis.daily[column], label=label, color=color, linewidth=width)
    axis.set_title(_title(synthetic) + "\n" + DISCLAIMER, fontsize=12, pad=12)
    axis.set_ylabel("Index points")
    axis.set_xlabel("Observation date (state uses only data through this date)")
    axis.xaxis.set_major_locator(mdates.YearLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axis.set_xlim(diagnosis.daily.iloc[0]["date"], diagnosis.spells.iloc[-1]["plot_end"])
    axis.grid(axis="y", alpha=0.2)
    handles, labels = axis.get_legend_handles_labels()
    handles += [Patch(facecolor=COLORS[state], alpha=0.35, label=state) for state in DIAGNOSTIC_REGIMES]
    axis.legend(handles=handles, loc="upper left", ncol=7, fontsize=9)
    figure.tight_layout()
    return figure


def _build_statistics_figure(diagnosis: MarketRegimeDiagnosis, synthetic: bool = False):
    figure, axes = plt.subplots(3, 2, figsize=(15, 12))
    summary = diagnosis.summary.set_index("regime").reindex(DIAGNOSTIC_REGIMES)
    colors = [COLORS[state] for state in DIAGNOSTIC_REGIMES]
    positions = np.arange(len(DIAGNOSTIC_REGIMES))
    shares, durations, annual, transitions, returns, volatility = axes.ravel()
    shares.bar(positions, summary["share"] * 100, color=colors)
    shares.set_title("Observed state share (UNKNOWN included)")
    shares.set_ylabel("% of report sessions")
    for i, row in enumerate(summary.itertuples()):
        shares.text(i, row.share * 100, "n=%d" % row.sessions, ha="center", va="bottom", fontsize=9)
    shares.margins(y=0.18)
    durations.bar(positions - 0.18, summary["mean_spell_sessions"], 0.36, label="Mean observed", color=colors)
    durations.bar(positions + 0.18, summary["max_spell_sessions"], 0.36, label="Max observed", color=colors, alpha=0.4)
    durations.set_title("Contiguous spell durations (boundary spells censored)")
    durations.set_ylabel("Trading sessions")
    durations.legend(fontsize=9)
    years = sorted(diagnosis.annual["year"].unique())
    annual_table = diagnosis.annual.pivot(index="year", columns="regime", values="share").reindex(columns=DIAGNOSTIC_REGIMES)
    bottom = np.zeros(len(years))
    for state in DIAGNOSTIC_REGIMES:
        heights = annual_table[state].to_numpy() * 100
        annual.bar([str(y) for y in years], heights, bottom=bottom, color=COLORS[state], label=state)
        bottom += heights
    annual.set_title("Annual state distribution (boundary years partial)")
    annual.set_ylabel("% of observed yearly sessions")
    annual.legend(ncol=4, fontsize=8, loc="lower left")
    matrix = diagnosis.transitions.pivot(index="from_regime", columns="to_regime", values="count").reindex(index=DIAGNOSTIC_REGIMES, columns=DIAGNOSTIC_REGIMES).fillna(0)
    transitions.imshow(matrix.to_numpy(), cmap="Blues")
    for i in range(4):
        for j in range(4):
            transitions.text(j, i, str(int(matrix.iloc[i, j])), ha="center", va="center")
    transitions.set_xticks(positions, DIAGNOSTIC_REGIMES, fontsize=9)
    transitions.set_yticks(positions, DIAGNOSTIC_REGIMES, fontsize=9)
    transitions.set_xlabel("To state")
    transitions.set_ylabel("From state")
    transitions.set_title("Adjacent state switches; UNKNOWN boundaries separate")
    returns.bar(positions, summary["mean_daily_return"] * 100, color=colors)
    returns.axhline(0, color="black", linewidth=0.8)
    returns.set_title("Same-day conditional mean return r[t] | state[t]")
    returns.set_ylabel("% per observed day (not forecast)")
    for i, row in enumerate(summary.itertuples()):
        if pd.notna(row.mean_daily_return):
            returns.annotate("n=%d" % row.return_samples, (i, row.mean_daily_return * 100),
                             xytext=(0, 4 if row.mean_daily_return >= 0 else -12), textcoords="offset points", ha="center", fontsize=9)
    returns.margins(y=0.2)
    volatility.bar(positions, summary["annualized_volatility_252"] * 100, color=colors)
    volatility.set_title("Conditional daily sample volatility × sqrt(252)")
    volatility.set_ylabel("Annualized % (scaling convention, not forecast)")
    for axis in (shares, durations, returns, volatility):
        axis.set_xticks(positions, DIAGNOSTIC_REGIMES, fontsize=9)
        axis.grid(axis="y", alpha=0.15)
    figure.suptitle(_title(synthetic) + "\n" + DISCLAIMER, fontsize=13)
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    return figure


def _markdown_table(table: pd.DataFrame) -> str:
    def format_value(value):
        if pd.isna(value):
            return "—"
        if isinstance(value, (float, np.floating)):
            return "%.6f" % value
        return str(value)
    lines = ["| " + " | ".join(map(str, table.columns)) + " |",
             "| " + " | ".join("---" for _ in table.columns) + " |"]
    lines += ["| " + " | ".join(format_value(value) for value in row) + " |"
              for row in table.itertuples(index=False, name=None)]
    return "\n".join(lines)


def write_market_regime_report(
    diagnosis: MarketRegimeDiagnosis, output_dir: Path, synthetic: bool = False,
) -> Dict[str, Path]:
    """Write once into a run-owned directory; every artifact is exclusive."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {}
    tables = {"daily": diagnosis.daily, "state_summary": diagnosis.summary,
              "spells": diagnosis.spells, "annual": diagnosis.annual,
              "transitions": diagnosis.transitions, "coverage": pd.DataFrame([{
                  key: value for key, value in diagnosis.coverage.items() if key != "unknown_reasons"
              }])}
    for name, table in tables.items():
        path = destination / (name + ".csv")
        labeled = table.copy()
        labeled["dataset_kind"] = "SYNTHETIC_SOFTWARE_CHECK" if synthetic else "CACHED_HISTORICAL_INDEX"
        with path.open("x", encoding="utf-8", newline="") as stream:
            labeled.to_csv(stream, index=False, date_format="%Y-%m-%d")
        outputs[path.name] = path
    for name, builder in (("market_regime_history.png", _build_history_figure),
                          ("market_regime_statistics.png", _build_statistics_figure)):
        figure = builder(diagnosis, synthetic)
        try:
            path = destination / name
            with path.open("xb") as stream:
                figure.savefig(stream, format="png", dpi=150,
                               metadata={"Description": _title(synthetic) + "; " + DISCLAIMER})
            outputs[name] = path
        finally:
            plt.close(figure)
    coverage = diagnosis.coverage
    lines = [
        "# 中证500市场状态历史诊断", "",
        "**历史描述性统计，不是预测收益。NO LIVE MONEY。**", "",
        "**SYNTHETIC SOFTWARE CHECK：人工价格及工作日日历，不是真实中证500行情，不可作研究结论。**" if synthetic
        else "数据：本地已验证的中证500行情缓存；本入口不联网、不下载、不训练模型。", "",
        "区间：%s 至 %s；样本 %d 日；可用预热 %d 日。" % (coverage["requested_start"], coverage["requested_end"], coverage["sessions"], coverage["warmup_sessions"]),
        "价格覆盖 %.2f%%，状态覆盖 %.2f%%，收益覆盖 %.2f%%（%d 个日收益）。" % (coverage["price_coverage"] * 100, coverage["state_coverage"] * 100, coverage["return_coverage"] * 100, coverage["return_samples"]),
        "日历分母：`%s`。" % coverage["calendar_basis"],
        "未提供独立交易日历时，100%仅表示缓存可观测日期内完整，不能证明中间无漏日或交易所完整覆盖。",
        "",
        "冻结规则：BULL 为 adj_close > MA60 且 MA20 > MA60；BEAR 为两个严格小于；完整窗口的其余情况为 SIDEWAYS。",
        "所有窗口向后包含 t。MA60 不足、价格缺失或窗口缺口只在 `DIAGNOSTIC_REGIME` 标注 UNKNOWN；",
        "`daily.csv` 同时保留原函数 `MARKET_REGIME`，不修改正式定义。未知原因：`%s`。" % coverage["unknown_reasons"],
        "",
        "![指数与状态背景](market_regime_history.png)", "",
        "![描述性统计](market_regime_statistics.png)", "",
        "## 状态占比、持续时长与条件收益", "",
        "份额分母包含 UNKNOWN。收益 `r[t] = adj_close[t]/adj_close[t-1]-1`，按同日 `state[t]` 分组；",
        "两者使用同日价格，存在机械关联，不能解释为可交易预测或前瞻收益。没有计算未来收益、策略收益或超额收益。",
        "均值及波动率是小数单位；波动率为样本标准差 ddof=1，年化仅乘 sqrt(252)，不是收益预测。",
        "有效收益少于2时波动率不可估计，空状态/不可估计值以 — 表示。样本量不是独立观测数，不做显著性结论。", "",
        _markdown_table(diagnosis.summary), "",
        "持续期以连续交易日段统计，UNKNOWN 中断，不跨缺口拼接。边界及邻 UNKNOWN 的段标注 censored，",
        "均值/中位数/最大值仅是已观测长度，不能视为完整状态寿命或预测持续时间。完整段见 `spells.csv`。", "",
        "## 转换与年度分布", "",
        "相邻已知状态转换 %d 次；UNKNOWN 边界 %d 次，后者单列，不计为 BULL/BEAR/SIDEWAYS 转换。" % (coverage["state_switches"], coverage["unknown_boundaries"]), "",
        _markdown_table(diagnosis.transitions), "",
        "年度份额按各年报告内可观测日期计算；首尾年是部分年份。", "",
        _markdown_table(diagnosis.annual[["year", "regime", "sessions", "share", "return_samples"]]), "",
        "## 数据与结果边界", "",
        "这份独立指数诊断不要求/验证历史500成员，不解除 PIT 门禁，不代表五年四模型实证或 Forward Test。",
        "缓存哈希固定本次读取的字节，不能证明供应商未事后修订数据。现有缓存缺失/损坏将失败，不回退联网或人工数据。",
        "CSV、PNG 与本报告共享同一诊断结果。配置、数据/源码/产物哈希及运行耗时见本目录 `manifest.json`；仅 status=success 的运行完整。",
    ]
    path = destination / "report.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    outputs[path.name] = path
    return outputs
