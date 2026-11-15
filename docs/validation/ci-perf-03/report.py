"""Render tables from profile.py outputs; no additional measurement."""
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(str(x) for x in row) + ' |' for row in rows])


def main():
    records = [json.loads(p.read_bytes()) for p in sorted(HERE.glob('run-*.json'))]
    primary = records[-1]
    text = ['# 计算结果', '', '单位均为秒，除非列名另有说明。预测为模块级离线模型，不是新分片实测。', '', '## 六分片真实基线', '']
    for r in records:
        text += [f'### Run {r["run_id"]}', '', table(['Python', '片', '测试数', 'JUnit本体', 'suite', 'receipt', '测试步骤', '安装', 'job'],
            [[v, b['shard'], b['tests'], *[f'{b[k]:.2f}' for k in ('testcase_seconds', 'suite_seconds', 'receipt_seconds', 'test_step_seconds', 'install_seconds', 'job_seconds')]]
             for v, baseline in r['baseline'].items() for b in baseline]), '']
    text += ['## 最后成功运行的原始集合：三／四分片', '', table(['Python', '分片数', '各片本体预测', '最大', '不均衡率', '理论下界', '距下界', '共享3.13分配的最大值'],
        [[v, count, ', '.join(f'{x:.2f}' for x in p['loads_seconds']), f'{p["max_seconds"]:.2f}', f'{p["imbalance"]:.4%}',
          f'{p["lower_bound_seconds"]:.2f}', f'{p["bound_gap_seconds"]:.3f}', f'{p["shared_python313_assignment_metrics"]["max_seconds"]:.2f}']
         for v, d in primary['historical_inventory'].items() for count, p in d['scenarios']['raw:1']['plans'].items()]), '']
    for scope, title in [('historical_inventory', '7,712 条原始测量集合'), ('current_inventory', f'当前 {primary["target_count"]:,} 条集合')]:
        text += [f'## {title}：优化敏感性与资源成本', '', 'P01/P02/both 的 50% 仅为对应模块剩余成本假设；完整 0/25/50/75/100% 网格和所有模块分配见 JSON。', '',
            table(['Python', '情景', '片数', '最大本体', '测试总成本', 'Runner计算秒', '取整分钟', 'E2E无额外排队', 'E2E沿用其他job排队', '共享分配E2E'],
            [[v, scenario, count, *[f'{p[k]:.2f}' for k in ('max_seconds', 'total_seconds', 'runner_seconds_with_observed_overhead')],
              p['rounded_runner_minutes'], f'{p["modeled_e2e_seconds_no_other_job_queue"]:.2f}', f'{p["modeled_e2e_seconds_observed_other_job_queue"]:.2f}', f'{p["shared_assignment_e2e_seconds"]:.2f}']
             for v, d in primary[scope].items() for scenario in ('raw:1', 'p01:0.5', 'p02:0.5', 'both:0.5')
             for count, p in d['scenarios'][scenario]['plans'].items()]), '']
    text += ['## 当前集合覆盖与旧权重影响', '']
    for v, d in primary['current_inventory'].items():
        text += [f'- Python {v}: {d["measured_modules"]} 个完整来源匹配模块，{len(d["unmeasured_modules"])} 个 UNMEASURED；每个未知模块估计 {d["unknown_estimate_seconds"]:.3f} 秒。',
                 f'- 旧分配按当前成本重估最大 {d["old_plan_evaluated_with_current_costs"]["max_seconds"]:.2f} 秒；新三分片最大 {d["scenarios"]["raw:1"]["plans"]["3"]["max_seconds"]:.2f} 秒；{len(d["modules_moved"])} 个模块改变片号。',
                 '- 未测量模块：' + (', '.join('`' + m + '`' for m in d['unmeasured_modules']) or '无')]
    text += ['', '## 两版本合计 Runner 成本（原始情景）', '', table(['集合', '每版本片数', 'extended 总计算秒', 'extended 取整分钟', '全运行计算秒', '全运行取整分钟'],
        [[scope, count, f'{sum(primary[scope][v]["scenarios"]["raw:1"]["plans"][count]["runner_seconds_with_observed_overhead"] for v in ("3.13", "3.14")):.2f}',
          sum(primary[scope][v]['scenarios']['raw:1']['plans'][count]['rounded_runner_minutes'] for v in ('3.13', '3.14')),
          f'{sum(primary[scope][v]["scenarios"]["raw:1"]["plans"][count]["runner_seconds_with_observed_overhead"] for v in ("3.13", "3.14")) + primary["overhead"]["other_jobs_runner_seconds"]:.2f}',
          sum(primary[scope][v]['scenarios']['raw:1']['plans'][count]['rounded_runner_minutes'] for v in ('3.13', '3.14')) + primary['overhead']['other_jobs_rounded_minutes']]
         for scope in ('historical_inventory', 'current_inventory') for count in ('3', '4')]), '']
    rows = list(csv.DictReader((HERE / 'module-timings.csv').open(encoding='utf-8')))
    selected = {v: {r['module']: float(r['measured_seconds']) for r in rows if r['run_id'] == primary['run_id'] and r['python'] == v and r['measured_seconds']} for v in ('3.13', '3.14')}
    modules = sorted(selected['3.13'], key=lambda m: -selected['3.13'][m])
    text += ['', '## 最慢模块与 Python 差异', '', table(['模块', '3.13', '3.14', '3.14 - 3.13'],
        [[m, f'{selected["3.13"][m]:.3f}', f'{selected["3.14"][m]:.3f}', f'{selected["3.14"][m]-selected["3.13"][m]:+.3f}'] for m in modules[:15]]), '']
    o = primary['overhead']
    text += ['## 端到端 15 分钟预算', '',
        f'最后成功运行实测端到端 {o["observed_e2e_seconds"]:.0f} 秒。按该运行准备/门禁包络：', '',
        f'`900 - {o["prefix_seconds"]:.0f}（risk前缀）- {o["dispatch_seconds"]:.0f}（分派）- {o["coverage_and_gate_tail_seconds"]:.0f}（coverage与gate尾部）- {o["max_non_test_job_seconds"]:.0f}（job非测试）= {o["max_test_step_budget_seconds"]:.0f} 秒测试步骤预算`。', '',
        f'再扣最大步骤残差 {o["max_test_step_residual_seconds"]:.3f} 秒，本体预算为 **{o["max_testcase_budget_seconds"]:.3f} 秒/片**。其他并行job原始完成时刻为 +{o["other_parallel_jobs_observed_finish_seconds"]:.0f} 秒；无新增排队时该路径尚未突破 15 分钟。', '',
        '三分片原始总工作量下界已超过预算，单靠重排不能达到目标。四分片需同时考察 Python 3.14 和共享分配，不能只看 3.13 的接近 10 分钟结果。largest-module 下界同时见各情景 JSON；当前最慢模块仍低于单片预算，主要约束是总工作量与未知模块储备。', '',
        f'其他固定 jobs 的实际计算合计 {o["other_jobs_runner_seconds"]:.0f} 秒、逐 job 取整 {o["other_jobs_rounded_minutes"]} 分钟；资源表为单个 Python 的 extended 分片成本，两版本相加再加这些固定成本得到整个运行模型。4 片比 3 片每版本增加一份准备/残差包络，不减少测试总工作量。', '',
        '原始分片的共享 fixture 成本、Runner 波动和队列不会因离线模型消失；建议以四分片＋优化情景作为后续工作流实验候选，而不是把预测作为 15 分钟实绩。']
    (HERE / 'RESULTS.md').write_text('\n'.join(text) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
