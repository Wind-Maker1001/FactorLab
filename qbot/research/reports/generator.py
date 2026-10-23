"""CSV, Markdown, and headless plot generation for Phase 1."""

from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from qbot.research.metrics.factor import FactorEvaluation


def _bar_plot(values: pd.Series, title: str, ylabel: str, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(10, 5))
    values.plot(kind="bar", ax=axis, color="#35618f")
    axis.axhline(0.0, color="black", linewidth=0.8)
    axis.set_title(title)
    axis.set_ylabel(ylabel)
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)


def _correlation_plot(matrix: pd.DataFrame, destination: Path) -> None:
    figure, axis = plt.subplots(figsize=(10, 8))
    image = axis.imshow(matrix.to_numpy(dtype=float), vmin=-1, vmax=1, cmap="coolwarm")
    axis.set_xticks(np.arange(len(matrix.columns)), matrix.columns, rotation=90)
    axis.set_yticks(np.arange(len(matrix.index)), matrix.index)
    axis.set_title("Mean Daily Cross-sectional Factor Correlation")
    figure.colorbar(image, ax=axis)
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)


def _quantile_plot(quantiles: pd.DataFrame, destination: Path) -> None:
    all_scope = quantiles.loc[quantiles["scope"] == "ALL"]
    means = all_scope.groupby(["factor", "quantile"], observed=False)["future_return"].mean().unstack()
    figure, axis = plt.subplots(figsize=(12, 6))
    means.plot(kind="bar", ax=axis)
    axis.axhline(0.0, color="black", linewidth=0.8)
    axis.set_title("Mean Forward Excess Return by Factor Quantile")
    axis.set_ylabel("Forward excess return")
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)


def _regime_plot(summary: pd.DataFrame, destination: Path) -> None:
    regimes = summary.loc[summary["scope"] != "ALL"].copy()
    figure, axis = plt.subplots(figsize=(12, 6))
    if regimes.empty:
        axis.text(
            0.5,
            0.5,
            "No market-regime data available",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
        axis.set_xticks([])
        axis.set_yticks([])
    else:
        table = regimes.pivot_table(
            index="factor",
            columns="scope",
            values="RankIC Mean",
            aggfunc="first",
        )
        table.plot(kind="bar", ax=axis)
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_title("Factor RankIC by CSI500 market regime")
        axis.set_ylabel("Mean daily Spearman RankIC")
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    plt.close(figure)


def write_factor_report(evaluation: FactorEvaluation, output_dir: Path) -> Path:
    """Write deterministic Phase-1 factor artifacts and return summary path."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    evaluation.summary.to_csv(destination / "factor_report.csv", index=False)
    evaluation.daily_ic.to_csv(destination / "daily_ic.csv", index=False)
    evaluation.quantile_returns.to_csv(destination / "quantile_returns.csv", index=False)
    evaluation.autocorrelation.to_csv(destination / "factor_autocorrelation.csv", index=False)
    evaluation.turnover.to_csv(destination / "factor_turnover.csv", index=False)
    evaluation.correlation.to_csv(destination / "factor_correlation.csv")

    all_summary = evaluation.summary.loc[evaluation.summary["scope"] == "ALL"].set_index("factor")
    if not all_summary.empty:
        _bar_plot(all_summary["IC Mean"], "Factor IC Mean", "Pearson IC", destination / "factor_ic.png")
        _bar_plot(
            all_summary["RankIC Mean"],
            "Factor RankIC Mean",
            "Spearman RankIC",
            destination / "rank_ic.png",
        )
    if not evaluation.correlation.empty:
        _correlation_plot(evaluation.correlation, destination / "factor_correlation.png")
    if not evaluation.quantile_returns.empty:
        _quantile_plot(evaluation.quantile_returns, destination / "quantile_returns.png")
    _regime_plot(evaluation.summary, destination / "regime_performance.png")

    autocorrelation = evaluation.autocorrelation
    turnover = evaluation.turnover
    auto_mean = (
        autocorrelation.loc[autocorrelation["scope"] == "ALL"]
        .groupby("factor")["autocorrelation"]
        .mean()
    )
    turnover_mean = (
        turnover.loc[turnover["scope"] == "ALL"]
        .groupby("factor")["top_quantile_turnover"]
        .mean()
    )
    lines = [
        "# FactorLab Phase 1 因子评价摘要",
        "",
        "本报告仅描述配置所指数据，不依据最终测试收益调参。ICIR 定义为日度 IC 均值除以日度 IC 标准差，未年化。",
        "分位成员只由当日因子值确定；quantile_returns.csv 同时记录未来标签已成熟数量与覆盖率，缺失标签不会触发事后补位。",
        "",
        "| Factor | IC Mean | ICIR | RankIC Mean | RankICIR | Coverage | Autocorr | Top-Q Turnover |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for factor, row in all_summary.iterrows():
        lines.append(
            "| %s | %.6f | %.6f | %.6f | %.6f | %.2f%% | %.6f | %.6f |"
            % (
                factor,
                row["IC Mean"],
                row["ICIR"],
                row["RankIC Mean"],
                row["RankICIR"],
                row["Coverage"] * 100.0,
                auto_mean.get(factor, np.nan),
                turnover_mean.get(factor, np.nan),
            )
        )
    summary_path = destination / "summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path
