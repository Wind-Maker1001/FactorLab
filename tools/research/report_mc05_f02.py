"""Render F02 model comparisons from completed offline evidence, without rerunning accounts."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

from factorlab.experiments.mc_grid_tail_convergence import write
from factorlab.research.monte_carlo.reliability import summarize_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    run = directory/'run'
    summary = json.loads((run/'summary.json').read_text())
    definition = json.loads((run/'definition.json').read_text())
    planning = json.loads((directory/'planning.json').read_text())
    rows = pd.read_csv(run/'paths.csv')
    evidence = json.loads((run/'path-evidence.json').read_text())
    subprocess.run([sys.executable, str(Path(__file__).with_name('report_mc05_f01.py')),
        '--run', str(run), '--output', str(directory/'STATISTICS.md'), '--research-id', 'MC-05-F02'], check=True)
    performance = summary['performance']
    complete = all(v == 'COMPLETED' for v in summary['model_execution_status'].values())
    lines = ['# MC-05-F02：替代模型 Grid Paper 尾部风险', '',
        'NO LIVE MONEY。FINANCIAL_NOT_ESTABLISHED。', '',
        '## 研究定义与资源', '',
        '冻结配置为 `configs/mc05_f02_grid_tail.json`。固定顺序 Jump-down → Regime historical → Moving-block bootstrap；'
        '每模型三个预定种子1729、2718、31415，每种子16路径，168小时，batch=8。只执行首档，不扩档，不混合模型风险分布。'
        'F01资产保持冻结，既有192个GBM与6个确定性账户均未重跑。', '',
        f"预校准6账户单独耗时{planning['total_seconds']:.4f}秒，正式样本不包含这些账户。"
        f"按较慢账户、生成和统计成本及1.2安全系数，144账户预计{planning['projected_144_account_seconds']:.4f}秒。"
        '正式预算900秒，其中预留10秒输出成本；每模型完整48账户整组准入，不按收益选择种子。'
        '预估RSS上限目标256MiB、临时磁盘8MiB、证据2MiB，均为资源规划值，不是统计精度承诺。', '',
        f"正式墙钟{performance['total_seconds']:.4f}秒，CPU {performance['cpu_seconds']:.4f}秒；"
        f"账户及账户诊断{performance['execution_seconds']:.4f}秒、清理{performance['cleanup_seconds']:.4f}秒、"
        f"生成及latent重放{performance['generation_seconds']:.4f}秒、统计{performance['statistics_seconds']:.4f}秒。"
        f"正式账户{performance['accounts']}；预算超额{performance['budget_overrun_seconds']:.4f}秒；"
        f"进程生命周期峰值RSS {performance['process_lifetime_peak_rss_bytes']:,}字节。", '',
        'Paper采用WAL+FULL，账户完成后立即删除可再生数据库。`planning.json`包含账户全流程及删除成本，'
        '`sqlite-wal-calibration.json`另列零账户的WAL+FULL创建/关闭/删除测量；'
        '`planning.json`中的空库微测量使用默认journal模式，不能当作WAL测量。'
        '内存只保留一个有界批次、紧凑账户行和各模型最差期末NAV路径。', '',
        '|模型|工程状态|首档账户|准入时已用秒|预计新增秒|',
        '|---|---|---:|---:|---:|']
    for model in definition['models']:
        decision = next((d for d in summary['decisions'] if d['model'] == model), None)
        count = int((rows.model == model).sum())
        elapsed = f"{decision['elapsed_seconds']:.4f}" if decision else '—'
        projected = f"{decision['projected_additional_seconds']:.4f}" if decision else '—'
        lines.append(f"|{model}|{summary['model_execution_status'][model]}|{count}|{elapsed}|{projected}|")
    lines += ['', f"历史校准拒绝原因：{summary['historical_calibration_rejection'] or '无'}。"
        '完整准入及资源拒绝记录见 `run/decisions.json`；未获准入模型不估计风险。', '',
        '## 校准与模型差异', '',
        '三模型共同使用已核准BINANCE_USDM_PUBLIC_DATA快照，历史观察窗口2026-05-14至2026-08-12，'
        '2159条小时简单收益；初始价格204.34，账户初始资本1000 USDT。数据版本及bars身份固定在配置中，正式入口校验身份。'
        '模拟起点2026-11-15晚于2026-11-14校准可用时间，不把5月至8月观察时间误称为当时已可研究获取。', '',
        '**Jump-down**：背景Gaussian log收益均值与样本波动由冻结历史估计；每小时共同Bernoulli事件概率1/168，'
        '每次log跳幅log(0.7)，即价格乘0.7，不做jump compensation。频率和幅度是人为压力假设，不是真实市场概率。'
        '模型版本、背景参数、来源及事件分布见配置和 `model-calibration.json`。', '',
        '**Regime historical**：24小时滞后波动及趋势划分low_down/low_up/high_down/high_up；'
        '波动阈值只由2026-06-28及以前的观察前缀确定，不根据模拟结果选阈值。'
        '状态标签只使用block之前的24条收益；后缀池及相隔24条收益的状态转移由历史估计，重叠样本不是独立转移。'
        '实际可用时间仍按采集时间处理，不声称2026-06-28已实时可用。模型校准收据记录阈值可用时间、各状态池、转移支持和矩阵。', '',
        '**Moving-block bootstrap**：24小时连续历史block均匀有放回抽样，保留块内时序依赖；'
        '块连接处打断依赖，不保留无限期波动聚集，未对block长度作结果驱动优化。'
        '与Regime使用同一冻结样本，但不按状态分池；与Jump的参数化事件机制不可互换。', '',
        f"历史lag1收益相关{planning['history']['autocorrelations']['1']['returns']:.6f}、平方收益相关"
        f"{planning['history']['autocorrelations']['1']['squared_returns']:.6f}；"
        f"最差小时收益{planning['history']['worst_hour']:.6%}，最差连续24小时复合收益"
        f"{planning['history']['worst_24h_compound']:.6%}。更多lag6/24诊断见规划收据。", '',
        '## 逐模型账户结果', '',
        '|模型|成交/总数|零成交率|曾库存比例|期末库存比例|期末NAV均值/最低|最大回撤峰值|最低路径NAV|平均/峰值利用率|费用/滑点合计|',
        '|---|---|---:|---:|---:|---|---:|---:|---|---|']
    for model in definition['models']:
        r = rows[rows.model == model]
        if r.empty:
            continue
        lines.append(f"|{model}|{r.traded.sum()}/{len(r)}|{r.zero_fills.mean():.4%}|"
            f"{r.ever_inventory.mean():.4%}|{r.has_terminal_inventory.mean():.4%}|"
            f"{r.terminal_nav.mean():.6f}/{r.terminal_nav.min():.6f}|{r.maximum_drawdown.max():.6%}|"
            f"{r.minimum_nav.min():.6f}|{r.capital_utilization_mean.mean():.4%}/{r.capital_utilization_peak.max():.4%}|"
            f"{r.fees.sum():.6f}/{r.slippage.sum():.6f}|")
    lines += ['', '各模型的统计可靠性均须结合实际条件样本量；已完成模型仅有3个种子、每种子16路径。'
        '四条件组的逐种子VaR95/CVaR95/VaR99/CVaR99、经验bootstrap区间、有效尾部质量、种子SD见 [STATISTICS.md](STATISTICS.md)。'
        'VaR二项次序统计区间及无穷端点见 `run/groups.json`：null的无界端点不是零。', '',
        '|模型|最大库存|平均/最大库存年龄(小时，观察最大值)|风险退出触发|FIFO已实现净损益合计|残余库存净损益合计|最终库存合计|残差绝对值最大|',
        '|---|---:|---|---:|---:|---:|---:|---:|']
    for model in definition['models']:
        r = rows[rows.model == model]
        if r.empty:
            continue
        lines.append(f"|{model}|{r.max_inventory.max():.4f}|{r.inventory_mean_age_hours.max():.4f}/{r.inventory_max_age_hours.max():.4f}|"
            f"{r.exit_triggers.sum()}|{r.fifo_realized_pnl.sum():.8f}|{r.fifo_unrealized_pnl.sum():.8f}|"
            f"{r.terminal_inventory.sum():.4f}|{r.reconciliation_residual.abs().max():.12g}|")
    lines += ['', 'FIFO已实现净损益为normal_sell_net+risk_exit_net；残余库存净损益含未平仓买入费用，'
        '两者与初始资本及估值调整共同核对最终NAV；`terminal_mark_unrealized_pnl`另保留账户按均价的未实现值。'
        '库存合计是跨独立账户加总，不是一个账户仓位；utilization是持仓市值/初始资金，不含未成交挂单冻结金额。', '',
        '## 风险驱动诊断', '']
    jump = [e for p in evidence if p['model'] == 'jump_down' for e in p['events']]
    jump_counts = [len(p['events']) for p in evidence if p['model'] == 'jump_down']
    if jump_counts:
        lines += [f"Jump共观察{len(jump)}次下跌事件；无跳跃路径{sum(n == 0 for n in jump_counts)}条，"
            f"单路径最多{max(jump_counts)}次。每次事件的小时索引、固定log跳幅、叠加背景后的实际价格缺口保存在 `run/path-evidence.json`。"
            '这些索引可与冻结起点和168小时步长复原跳跃时间。最差期末NAV价格路径见 `run/tails.json`，可检查跳后恢复及期末残余库存。', '']
    regime_paths = [p for p in evidence if p['model'] == 'regime_historical']
    events = [e for p in regime_paths for e in p['events']]
    if events:
        conditional = []
        for seed in definition['experiment']['seeds']:
            for state in definition['model_parameters']['regime_historical']['states']:
                sample = [[e for e in p['events'] if e['state'] == state]
                    for p in regime_paths if p['seed'] == seed]
                sample = [blocks for blocks in sample if blocks]
                if sample:
                    conditional.append(dict(seed=seed, state=state, n=len(sample),
                        statistics=summarize_paths([-sum(e['nav_change'] for e in blocks)/1000 for blocks in sample],
                            [max(e['maximum_drawdown'] for e in blocks) for blocks in sample],
                            resamples=definition['experiment']['resamples'], bootstrap_seed=seed)))
                else:
                    conditional.append(dict(seed=seed, state=state, n=0, status='INSUFFICIENT_DATA'))
        write(directory/'regime-state-statistics.json', conditional)
        lines += ['状态条件尾部另见 `regime-state-statistics.json`：每种子、每状态，以每条独立路径在该状态全部block的NAV变化之和取负并除1000，'
            '仅纳入出现该状态的路径，复用MC-02 VaR/CVaR95/99与不确定性方法。'
            '这是状态期间损益贡献的条件统计，不是独立状态账户的终值损失；持仓跨状态，不能用它解释因果或混合成市场CVaR。'
            '其尾部样本仍不足。', '', '|生成block状态|block数|状态小时|成交小时/状态小时|库存小时/状态小时|库存峰值|块内NAV变化最低/中位数|全路径回撤观察峰值|',
            '|---|---:|---:|---|---|---:|---|---:|']
        for state in definition['model_parameters']['regime_historical']['states']:
            blocks = [e for e in events if e['state'] == state]
            if not blocks:
                continue
            hours = sum(e['size'] for e in blocks)
            changes = [e['nav_change'] for e in blocks]
            lines.append(f"|{state}|{len(blocks)}|{hours}|{sum(e['traded_hours'] for e in blocks)}/{hours}|"
                f"{sum(e['inventory_hours'] for e in blocks)}/{hours}|{max(e['inventory_peak'] for e in blocks):.4f}|"
                f"{min(changes):.6f}/{np.median(changes):.6f}|{max(e['maximum_drawdown'] for e in blocks):.6%}|")
        switches = sum(sum(a['state'] != b['state'] for a,b in zip(p['events'],p['events'][1:])) for p in regime_paths)
        durations = []
        for p in regime_paths:
            current, duration = None, 0
            for e in p['events']:
                if e['state'] != current and current is not None:
                    durations.append(duration)
                    duration = 0
                current = e['state']
                duration += e['size']
            durations.append(duration)
        lines += ['', f"生成状态切换{switches}次；状态连续段持续时间中位数{np.median(durations):.1f}小时、最大{max(durations)}小时。"
            '状态在24小时block边界变化，持续时间受到24小时离散化及168小时窗口截断；'
            '以上为生成器选池状态，不是对事后模拟收益重新分割。块内NAV变化不等于账户终值损失，block也非独立尾部样本。', '']
    lines += ['|模型|路径lag1收益相关均值|路径lag1平方收益相关均值|最差小时观察|最差24小时观察|',
        '|---|---:|---:|---:|---:|']
    for model in definition['models']:
        r = rows[rows.model == model]
        if not r.empty:
            lines.append(f"|{model}|{r.return_autocorrelation_1.mean():.6f}|{r.squared_return_autocorrelation_1.mean():.6f}|"
                f"{r.worst_hour_return.min():.6%}|{r.worst_24h_return.min():.6%}|")
    lines += ['', 'Bootstrap极端片段保留程度由冻结历史与生成路径的最差小时/24小时复合收益对照；'
        '24小时窗口可能跨抽样块边界，不能把合成连续片段误称为原始历史片段。块内时序保持并不保证有限样本抽中每个极端片段。'
        '三模型路径数量及独立种子相同，但校准方法、生成机制、实际成交和敞口不同；金融优劣不能只按回撤排序。', '',
        '## 统计与金融解释', '',
        '每种子16路径的95%有效尾部质量仅0.8、99%仅0.16，条件组更少；'
        'CVaR可能完全由单个最差账户决定。经验bootstrap只重采样已观察支持集，无法补足未见尾部。'
        '种子之间离散度是描述性差异，不是市场预测区间；本轮没有32/64档序列，不能宣称收敛。'
        '零成交是未参与，不是策略成功；低持仓敞口下的小回撤不是风险控制优于满仓基准的证据。'
        'Jump库存损失与价格缺口、Regime状态条件下的成交和库存、Bootstrap历史依赖共同构成压力情景证据，'
        '不构成外部真实市场验证。', '',
        f"ENGINEERING_EXECUTION_STATUS: {summary['engineering_status']}", '',
        f"MODEL_COVERAGE_STATUS: {'THREE_MODELS_FIRST_TIER_COMPLETED' if complete else 'PARTIAL_MODEL_COVERAGE'}", '',
        f"STATISTICAL_RELIABILITY_STATUS: {summary['statistical_status']}", '',
        'CONVERGENCE_STATUS: CONVERGENCE_NOT_ESTABLISHED', '',
        'FINANCIAL_NOT_ESTABLISHED', '', 'NO LIVE MONEY', '',
        '## 验证与账户审计', '',
        '正式前定向回归151项通过（40.52秒），另新增的无确定性账户配置约束测试单独通过。'
        '初始新增状态关联测试曾因测试价格区间与准入模式设置错误失败，修正fixture后通过；'
        '覆盖MC-05调度/整组准入/中断、MC-02尾部、MC-03生成器、Grid Paper和FIFO/NAV相关回归。', '',
        '实际证据审计见 `audit.json`：144个唯一账户、9个完整种子组，每组path_id为0至15；'
        '所有归因残差为0，无持久化SQLite，正式运行代码收据git_dirty=false。'
        '正式run证据811234字节，单账户临时输出观察峰值128374字节；F01冻结路径没有变更。', '',
        '## 重放', '',
        '在冻结代码环境运行；重放输出应使用新目录，不覆盖正式证据。', '', '```bash',
        'PYTHONPATH=src python tools/research/plan_mc05_f02.py \\',
        '  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \\',
        '  --output mc05-f02-planning-replay',
        'PYTHONPATH=src python tools/research/run_mc_grid_tail_convergence.py \\',
        '  --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 \\',
        '  --definition configs/mc05_f02_grid_tail.json --output mc05-f02-replay',
        'PYTHONPATH=src python tools/research/report_mc05_f02.py --directory docs/validation/mc-05-f02', '```', '',
        '正式运行代码、解释器及命令见 `run/definition.json`。资源准入取决于运行机器实际耗时，重放不保证相同墙钟。', '']
    (directory/'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
