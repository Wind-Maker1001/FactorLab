"""Render MC-05-F01 evidence tables from completed account receipts."""
import argparse
import json
from pathlib import Path

import pandas as pd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--research-id', default='MC-05-F01')
    args = parser.parse_args()
    run = Path(args.run)
    summary = json.loads((run/'summary.json').read_text())
    cells = json.loads((run/'groups.json').read_text())
    rows = pd.read_csv(run/'paths.csv')
    lines = [f'# {args.research_id} 详细统计', '',
        '损失和回撤以初始资本比例表示；金额单位USDT。区间为95%条件模拟区间，不是市场预测区间。', '',
        '## 每种子、规模、参与条件的尾部与区间', '',
        '|模型|规模|种子|条件|n|VaR95|CVaR95 [区间]|VaR99|CVaR99 [区间]|95/99尾部质量|95/99非零权重路径|',
        '|---|---:|---:|---|---:|---:|---|---:|---|---|---|']
    for cell in cells:
        for group, value in cell['groups'].items():
            prefix = f"|{cell['model']}|{cell['n_paths']}|{cell['seed']}|{group}|{value['n']}|"
            if not value['n']:
                lines.append(prefix+'INSUFFICIENT_DATA||||||')
                continue
            tails = value['statistics']['tails']
            a, b = tails['0.95'], tails['0.99']
            def interval(t):
                ci = t['cvar_interval']
                return f"{t['cvar']:.8f} [{ci['lower']:.8f}, {ci['upper']:.8f}]"
            lines.append(prefix+f"{a['var']:.8f}|{interval(a)}|{b['var']:.8f}|{interval(b)}|"
                f"{a['expected_tail_paths']:.3f}/{b['expected_tail_paths']:.3f}|"
                f"{a['tail_paths_with_nonzero_weight']}/{b['tail_paths_with_nonzero_weight']}|")
    lines += ['', 'VaR二项次序统计区间及无界端点、CVaR最大单路径权重、不同损失数和退化警告完整保存在 `run/groups.json`。非零权重路径数不等于独立尾部质量；小数尾部质量小于1时CVaR等于样本最大损失。所有本轮单种子尾部均为STATISTICALLY_UNSTABLE。', '',
        '## 跨种子离散度（样本标准差）', '',
        '|模型|规模|条件|种子|VaR95 SD|CVaR95 SD|VaR99 SD|CVaR99 SD|',
        '|---|---:|---|---|---:|---:|---:|---:|']
    for item in summary['seed_dispersion']:
        metrics = item['metrics']
        values = [str(metrics[key]['seed_sd']) if metrics else 'INSUFFICIENT_DATA'
            for key in ('var.95', 'cvar.95', 'var.99', 'cvar.99')]
        lines.append(f"|{item['model']}|{item['n_paths']}|{item['group']}|{item['seeds']}|"+'|'.join(values)+'|')
    lines += ['', '## 每档累计账户的参与及资源暴露', '',
        '|模型|每种子规模|账户|成交|曾库存|期末库存|亏损|零成交|平均利用率|最大利用率|平均敞口|峰值敞口|费用合计|滑点合计|期末库存合计|退出事件|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for model, size in sorted({(c['model'], c['n_paths']) for c in cells}):
        r = rows[(rows.model == model) & (rows.path_id < size)]
        lines.append(f"|{model}|{size}|{len(r)}|{r.traded.sum()}|{r.ever_inventory.sum()}|"
            f"{r.has_terminal_inventory.sum()}|{r.loss.sum()}|{r.zero_fills.sum()}|"
            f"{r.capital_utilization_mean.mean():.8f}|{r.capital_utilization_peak.max():.8f}|"
            f"{r.exposure_mean.mean():.6f}|{r.exposure_peak.max():.6f}|{r.fees.sum():.8f}|"
            f"{r.slippage.sum():.8f}|{r.terminal_inventory.sum():.4f}|{r.exit_triggers.sum()}|")
    lines += ['', '每档是嵌套累计，不可相加为独立账户总数。四类条件的亏损概率、零成交比例和参与概率的Wilson区间，最大回撤分布及区间，库存、敞口、利用率、退出、费用与滑点的分位数见 `run/groups.json`；逐账户FIFO/NAV残差见 `run/paths.csv`。', '',
        '## 相邻规模估计差异（上档减下档）', '',
        '|模型|种子|条件|规模|ΔVaR95|ΔCVaR95|ΔVaR99|ΔCVaR99|',
        '|---|---:|---|---|---:|---:|---:|---:|']
    for item in summary['convergence']:
        d = item['differences']
        lines.append(f"|{item['model']}|{item['seed']}|{item['group']}|{item['lower']}→{item['upper']}|"
            f"{d['0.95']['var']:.8f}|{d['0.95']['cvar']:.8f}|{d['0.99']['var']:.8f}|{d['0.99']['cvar']:.8f}|")
    lines += ['', '上述差异是配对嵌套样本的描述，不是独立样本检验。没有预设收敛精度验收阈值，且尾部质量不足，因此不能用差异小宣告收敛。', '',
        '## 账户核对', '',
        f"真实随机账户{len(rows)}；唯一账户标识{len(rows.drop_duplicates(['model', 'seed', 'path_id']))}；"
        f"最大FIFO/NAV归因残差绝对值{rows.reconciliation_residual.abs().max():.12g}。", '']
    Path(args.output).write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
