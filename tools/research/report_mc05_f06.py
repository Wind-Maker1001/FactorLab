"""Publish compact F06 evidence from complete local account populations."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil

import numpy as np

from factorlab.experiments.mc_grid_tail_convergence import write
from factorlab.research.grid_simulation import COLUMNS
from factorlab.research.monte_carlo import _loss_tail


def compact_evidence(source, destination):
    source, destination = Path(source), Path(destination)
    report = json.loads((source/'report.json').read_text(encoding='utf-8'))
    if report['status'] != 'COMPLETE':
        raise ValueError('a complete study is required for publication')
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source/'definition.json', destination/'definition.json')
    write(destination/'statistics.json', report)
    audit_cases, receipts, thin = [], [], []
    for completed in report['completed_cells']:
        model, seed = completed['model'], completed['seed']
        name = f"{model}-{seed}-{report['steps']}"
        directory = source/name
        manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        receipts.append({key: value for key, value in manifest.items() if key != 'audit'})
        audit = json.loads((directory/'paper-audits/audit.json').read_text(encoding='utf-8'))
        for case in audit['cases']:
            case = dict(case, model=model, seed=seed, steps=report['steps'])
            audit_cases.append(case)
            selected = destination/'selected-prices'/name
            selected.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(case['prices_file'], selected/f"path-{case['path_id']}.npy")
        terminal = np.load(directory/'terminal.npy', mmap_mode='r')
        losses = 1-terminal[:, COLUMNS.index('equity')]/1000
        rng = np.random.default_rng(np.random.SeedSequence([0x46303654, seed,
            report['models'].index(model), report['steps']]))
        draws = {str(c): [] for c in (.95, .99)}
        for _ in range(256):
            sample = losses[rng.choice(len(losses), 64, replace=False)]
            for c in (.95, .99):
                draws[str(c)].append(_loss_tail(sample, c)[1])
        thin.append(dict(model=model, seed=seed, steps=report['steps'], sample_n=64,
            repetitions=256, population_n=len(losses),
            meaning='random subsets of the new empirical population; not old paths or market calibration uncertainty',
            cvar={str(c): dict(population=_loss_tail(losses, c)[1],
                subset_mean=float(np.mean(draws[str(c)])),
                subset_sd=float(np.std(draws[str(c)], ddof=1)),
                subset_q025=float(np.quantile(draws[str(c)], .025)),
                subset_q975=float(np.quantile(draws[str(c)], .975)),
                subset_maximum=float(max(draws[str(c)]))) for c in (.95, .99)}))
    write(destination/'generation-receipts.json', receipts)
    write(destination/'paper-audits.json', audit_cases)
    write(destination/'thin-sample-diagnostics.json', thin)
    return report, audit_cases, thin


def report_markdown(studies, destination):
    destination = Path(destination)
    lines = ['# MC-05-F06：多模型网格尾部风险研究', '',
        '本报告为历史校准条件下的合成路径研究，不是真实行情回放。报价先冻结六位小数；不声称旧MC-05逐路径复现。资金费率未模拟，净收益指本研究执行假设下的权益变动。', '',
        '## 全样本结果', '',
        '每种子独立保留原始终态，三个规模档位严格嵌套。下表为各模型/周期最大档五种子指标的均值；种子范围及逐档点态区间见统计JSON。VaR/CVaR为初始权益比例损失，非仅成交账户。', '',
        '| 周期h | 模型 | 每种子账户 | 种子数 | 均值净收益% | 亏损概率% | 零成交% | VaR99% | CVaR99% | CVaR99种子范围% |',
        '|---:|---|---:|---:|---:|---:|---:|---:|---:|---|']
    result_rows, audit_all, thin_all = [], [], []
    for report, audits, thin in studies:
        audit_all.extend(audits)
        thin_all.extend(thin)
        groups = defaultdict(list)
        maximum = max(report['sizes'])
        for cell in report['cells']:
            if cell['n_paths'] == maximum:
                groups[cell['model']].append(cell)
        for model, cells in groups.items():
            g = [cell['groups']['unconditional'] for cell in cells]
            mean = lambda values: float(np.mean(values))
            tails = [v['tails']['0.99']['cvar'] for v in g]
            values = [mean([v['net_return']['mean']['estimate'] for v in g]),
                mean([v['events']['loss']['joint']['estimate'] for v in g]),
                mean([v['events']['zero_fill']['joint']['estimate'] for v in g]),
                mean([v['tails']['0.99']['var'] for v in g]), mean(tails)]
            lines.append(f"| {report['steps']} | {model} | {maximum:,} | {len(cells)} | " +
                ' | '.join(f'{v*100:.6f}' for v in values) + f' | {min(tails)*100:.6f}–{max(tails)*100:.6f} |')
            result_rows.append(dict(steps=report['steps'], model=model, n=maximum,
                seeds=len(cells), mean_return=values[0], loss_probability=values[1],
                zero_fill_probability=values[2], var99=values[3], cvar99=values[4]))
    lines += ['', '## 抽样精度与薄尾部', '',
        '所有点估计从完整总体计算，不平均批次尾部。概率采用Wilson区间，VaR采用二项秩区间，CVaR使用正则性诊断下的影响函数大样本区间。并列阈值、薄尾部或退化支持导致未解决时不输出伪精确区间。所有区间为模型/校准/执行假设条件下的模拟抽样精度，不覆盖模型风险或校准误差。', '',
        '每个模型/种子从新总体独立抽取256个64账户子集，记录CVaR波动。64账户在99%尾部只有0.64账户质量，CVaR退化为样本最大损失；这能解释薄样本的不稳定机制，但不识别旧F03的全部误差来源。详见thin-sample-diagnostics.json。', '',
        '没有预先给定金融容差，统计区间收窄不构成正式收敛或金融等价结论。跨种子范围、嵌套档位配对差异和所有条件分母保留在statistics.json。', '',
        '## 模型与经济解释', '',
        'GBM、历史区块、状态条件区块和下跳压力对尾部给出不同假设。Jump-down每小时1/168、30%下跳是人为压力设定，不能与真实市场发生概率等同。模型差异不能靠增加同一模型路径数消除。', '',
        '网格收益须同时观察成交账户与总体账户：零成交保留为总体的一部分，持仓未完成周期的mark损益不能称作已实现网格利润。总体终态占用资金/敞口与收益相关性见capital_return；它不是全程资金效率或因果回归。', '',
        '## TTL与长期风险', '',
        'TTL固定100、无续期。申请取消当步仍可能成交，反向订单有自己的创建时间；不能假设所有成交在第100小时截止。审计保留逐路径末次成交、TTL后成交及剩余持仓观察期。策略停滞后库存仍随价格计价，长期风险不等于继续获得循环收益。', '',
        '## 原始证据与独立审计', '',
        f'本轮保留{len(audit_all)}个模型/种子/周期/路径审计案例，其中独立随机和目的抽样标签分别记录。每个案例使用相同冻结价格执行批量/Paper逐步逐笔对照，再做文件Paper/FIFO归因。选择的极端路径不估计总体概率或CVaR。', '',
        '完整原始账户数组留在本地结果目录，紧凑统计、生成收据、审计数据及选中价格纳入Git。运行时间包括生成、执行、统计与审计，分别记录；没有云CU开销。']
    (destination/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    write(destination/'overview.json', result_rows)
    errors = [max(case['maximum_absolute_step_error_by_column'].values())
        for case in audit_all if case['maximum_absolute_step_error_by_column'] is not None]
    random_count = sum(case['independent_random'] for case in audit_all)
    audit_lines = ['# F06 Paper/FIFO 独立审计', '',
        f'共{len(audit_all)}个案例，独立随机案例{random_count}个（与极端/形态标签可能重合）。', '',
        f'批量与Paper逐步最大绝对列误差：{max(errors) if errors else None}。', '',
        '完整选择标签、冻结路径、逐笔成交、每列逐步误差及FIFO余额/库存/净值归因见各周期paper-audits.json及selected-prices。独立随机样本用于经济正确性核查；极端及形态样本为目的性诊断，不参与总体风险估计。', '',
        '审计的逐时资金占用和TTL诊断属于选样路径，不推广为全账户全程分布。无法支持的批量路径保留身份和拒绝原因，权威Paper补全时明确标记PAPER_ONLY，不记成批量一致性通过。']
    (destination/'PAPER_AUDIT.md').write_text('\n'.join(audit_lines)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--studies', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    studies = []
    for source in args.studies:
        report = json.loads((source/'report.json').read_text(encoding='utf-8'))
        studies.append(compact_evidence(source, args.output/f"horizon-{report['steps']}"))
    report_markdown(studies, args.output)


if __name__ == '__main__':
    main()
