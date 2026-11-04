"""Standalone CLI: python -m factorlab.strategies.dual_ma_cli --help."""
import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.errors import FactorLabError
from factorlab.metrics.risk import annualized_return, annualized_volatility, sharpe_ratio
from factorlab.strategies.dual_ma import DualMAConfig, run_software_backtest


def synthetic_bars(config):
    """Generated test observations; weekdays are NOT an exchange calendar."""
    dates = pd.bdate_range(pd.Timestamp(config.start) - pd.offsets.BDay(config.long_window + 5), config.end)
    x = np.arange(len(dates), dtype=float)
    close = 10 + 0.6 * np.sin(x / 4) + x * 0.001
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(date=dates, symbol=config.symbol, adj_close=close,
                             raw_open=opening, raw_close=close,
                             raw_high=np.maximum(opening, close) + 0.01,
                             raw_low=np.minimum(opening, close) - 0.01,
                             volume=1000000, trade_status=True, is_st=False)), dates


def round_trips(trades):
    rows, entry = [], None
    for trade in trades.itertuples(index=False):
        if trade.side == "buy":
            entry = trade
        elif entry is not None:
            pnl = trade.trade_notional - trade.transaction_cost - entry.trade_notional - entry.transaction_cost
            rows.append(dict(entry_date=entry.execution_date, exit_date=trade.execution_date,
                             shares=trade.executed_shares, net_pnl=pnl,
                             net_return=pnl / (entry.trade_notional + entry.transaction_cost)))
            entry = None
    return pd.DataFrame(rows, columns=["entry_date", "exit_date", "shares", "net_pnl", "net_return"])


def metrics(result, periods_per_year=252, risk_free_rate=0.0):
    nav, trades = result["nav"], result["trades"]
    returns = nav.strategy_return
    values = np.r_[1.0, nav.strategy_nav.to_numpy()]
    trips = round_trips(trades)
    return dict(cumulative_return=float(values[-1] - 1),
                annualized_return=annualized_return(returns, periods_per_year),
                annualized_volatility=annualized_volatility(returns, periods_per_year),
                sharpe_ratio=sharpe_ratio(returns, risk_free_rate, periods_per_year),
                maximum_drawdown=float(np.min(values / np.maximum.accumulate(values) - 1)),
                trading_sessions=len(nav), periods_per_year=periods_per_year,
                risk_free_rate=risk_free_rate, trade_count=len(trades),
                closed_round_trips=len(trips),
                trade_win_rate=None if trips.empty else float((trips.net_pnl > 0).mean()),
                total_fees=float(trades[["commission", "stamp_duty", "transfer_fee", "side_fee"]].sum().sum()),
                total_slippage=float(trades.slippage.sum()),
                total_transaction_cost=float(trades.transaction_cost.sum()),
                final_shares=int(nav.shares.iloc[-1]), final_cash=float(nav.cash.iloc[-1]))


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    return value


def _json(path, value):
    path.write_text(json.dumps(_clean(value), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_report(result, bars, calendar, config, fees, output, *, periods_per_year=252, risk_free_rate=0.0):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bars.to_csv(output / "input_bars.csv", index=False, float_format="%.17g")
    pd.DataFrame({"date": calendar}).to_csv(output / "calendar.csv", index=False)
    result["signals"].to_csv(output / "signals.csv", index=False)
    stats = {}
    for name in ("strategy", "benchmark"):
        leg = result[name]
        for key in ("nav", "trades", "rejections"):
            leg[key].to_csv(output / (name + "_" + key + ".csv"), index=False)
        round_trips(leg["trades"]).to_csv(output / (name + "_round_trips.csv"), index=False)
        stats[name] = metrics(leg, periods_per_year, risk_free_rate)
    _json(output / "metrics.json", stats)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 5))
    for name in ("strategy", "benchmark"):
        nav = result[name]["nav"]
        ax.plot(nav.date, nav.strategy_nav, label=name)
    ax.set(title="SYNTHETIC / SOFTWARE ONLY - Dual MA vs buy and hold", ylabel="NAV (initial capital = 1)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(output / "nav.png", dpi=140)
    plt.close(fig)
    package = Path(__file__).resolve().parents[1]
    code_hashes = {str(p.relative_to(package)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(package.rglob("*.py"))}
    repo = package.parent.parent
    head = None
    state = None
    if (repo / "pyproject.toml").is_file() and (repo / ".git").exists():
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        state = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).splitlines()
    manifest = dict(schema="FACTORLAB_DUAL_MA_SOFTWARE_V1", scope=result["scope"], synthetic=True,
                    no_live_money=True, pit="PIT_UNVERIFIED", hfq="HFQ_UNVERIFIED",
                    formal_admission="FORMAL_ADMISSION_BLOCKED", execution_admission=False,
                    config=asdict(config), fees=[{k: str(v) if k.startswith("effective_") and v is not None else v
                                               for k, v in asdict(rule).items()} for rule in fees.rules],
                    periods_per_year=periods_per_year, risk_free_rate=risk_free_rate,
                    code_commit=head, worktree_status=state, package_root=str(package), source_sha256=code_hashes,
                    python=sys.version, executable=sys.executable, platform=platform.platform(),
                    packages={name: importlib.metadata.version(name) for name in ("numpy", "pandas", "matplotlib")})
    text = """# 双均线软件验证报告

SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE / NO LIVE MONEY
PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

此报告仅使用程序生成的行情与工作日日历，不能作为中国石油真实历史收益。
参数、环境、源文件哈希和费用表见 manifest.json；完整指标见 metrics.json。
信号用 adj_close 的完整窗口简单均线；成交和估值分别用 raw_open、raw_close。
从空仓开始；不承接预热期信号；首日已有多头排列不买入。
收盘交叉最早下一交易日开盘执行。失败订单逐日重试，直到成交或被新目标替换。
目标正权重仅在空仓时按可用资金比例建仓；持有期间不做每日再平衡。
100% 目标扣除成本并向下取整，可能保留零钱。期末不强制清仓。
买入持有在区间第一个开盘下达事先已知的买入指令；使用相同仓位上限、整手、成本和拒单规则。
分红、送转、拆股账本未实现；非空公司行为或变化复权比例被拒绝，无真实总收益结论。
滑点沿用现有执行器：按成交额收取现金成本，execution_price 保留原始开盘价，不重复调整价格。
手续费采用 manifest 中既有研究费率和历史印花税表；并非历史券商费率认证。
涨跌停以昨日原始收盘估计，日线不能还原盘口队列。
年化收益 = (期末权益 / 初始资金)^(periods_per_year / N) - 1，N 含首个开盘至收盘样本。
年化波动率使用日收益样本标准差(ddof=1)乘 sqrt(periods_per_year)；夏普使用既有 risk 模块，
按日超额收益均值除以样本标准差，再乘 sqrt(periods_per_year)。默认无风险年利率为0。
最大回撤包含初始净值1。交易次数为成交订单数；胜率为已完成买卖回合扣除全部成本后
净盈亏严格大于0的比例；未平仓不计入，零回合输出 null。benchmark_* 文件保留基准完整账本。

![NAV](nav.png)
"""
    (output / "report.md").write_text(text, encoding="utf-8")
    manifest["artifacts_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted(output.iterdir()) if p.is_file()}
    _json(output / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dual MA: synthetic software verification; real execution remains gated")
    parser.add_argument("--config", type=Path, help="JSON config; explicit CLI values override config")
    parser.add_argument("--synthetic", action="store_true", help="generate test data internally; never reads real bars")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bars", type=Path, help="real data request (currently blocked by existing admission)")
    for name in ("symbol", "start", "end"):
        parser.add_argument("--" + name)
    for name in ("short_window", "long_window", "board_lot"):
        parser.add_argument("--" + name.replace("_", "-"), type=int)
    for name in ("initial_cash", "max_weight"):
        parser.add_argument("--" + name.replace("_", "-"), type=float)
    parser.add_argument("--periods-per-year", type=int, default=252)
    parser.add_argument("--risk-free-rate", type=float, default=0.0)
    parser.add_argument("--commission-bps", type=float, default=3.0)
    parser.add_argument("--minimum-commission-cny", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--transfer-fee-bps", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        if args.synthetic and args.bars is not None:
            parser.error("--synthetic cannot be combined with --bars")
        if not args.synthetic:
            from factorlab.security import assert_independent_admission
            assert_independent_admission()
        if args.periods_per_year <= 0 or not np.isfinite(args.risk_free_rate) or args.risk_free_rate <= -1:
            parser.error("invalid annualization/risk-free parameters")
        parameters = {} if args.config is None else json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(parameters, dict):
            raise ValueError("config must be a JSON object")
        parameters.update({k: getattr(args, k) for k in DualMAConfig.__dataclass_fields__
                           if hasattr(args, k) and getattr(args, k) is not None})
        config = DualMAConfig(**parameters)
        fees = FeeSchedule.a_share_default(args.commission_bps, args.minimum_commission_cny,
                                           args.slippage_bps, args.transfer_fee_bps)
        bars, calendar = synthetic_bars(config)
        result = run_software_backtest(bars, calendar, config, fee_schedule=fees)
        write_report(result, bars, calendar, config, fees, args.output,
                     periods_per_year=args.periods_per_year, risk_free_rate=args.risk_free_rate)
        print("SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE:", args.output.resolve())
        return 0
    except (FactorLabError, ValueError, TypeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
