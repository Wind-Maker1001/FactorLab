"""Audit and report complete model/seed prefixes from an MC-05-F03 run."""
from collections import Counter
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from factorlab._identity import source_context_root
from factorlab.experiments.mc_grid_tail_convergence import _replay_account, batches, price_digest, write
from factorlab.experiments.mc_grid_tail_f03_financial import capture_account_diagnostics, render_financial_report
from factorlab.experiments.mc_grid_tail_f03_statistics import analyze, render_report
from factorlab.experiments.mc_regime_jump import scenario_models
from factorlab.experiments.provenance import capture_provenance
from factorlab.research.regime_jump import PathPlan


FROZEN_RUN = Path('docs/validation/mc-05-f02/run')
KEYS = ['model', 'seed', 'path_id']
NON_ECONOMIC = ['temporary_account_bytes', 'reconciliation_residual']
CORRELATIONS = ['return_autocorrelation_1', 'squared_return_autocorrelation_1']


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _checkpoint_counts(cells, settings, models):
    counts = dict.fromkeys(models, 0)
    identities = Counter((cell['model'], cell['n_paths'], cell['seed']) for cell in cells)
    for model in models:
        for size in settings['sizes']:
            found = [identities[model, size, seed] for seed in settings['seeds']]
            if found == [1] * len(found):
                if counts[model] != settings['sizes'][settings['sizes'].index(size)-1] and size != settings['sizes'][0]:
                    raise ValueError(f'missing predecessor checkpoint for {model}/{size}')
                counts[model] = size
            elif any(found):
                raise ValueError(f'incomplete/duplicate seed checkpoint for {model}/{size}')
    return counts


def _generator_batch(model, inputs, generators, seed, begin, end, settings, definition):
    return batches(model, inputs, generators.get(model), seed=seed, begin=begin, end=end,
        batch_size=settings['batch_size'], steps=settings['steps'],
        block_size=definition['bootstrap']['block_size'])


def _audit_paths(frame, path_evidence, *, inputs, receipt, config, rules, definition, completed):
    settings = definition['experiment']
    generators, _ = scenario_models(inputs, receipt, threshold_as_of=definition['threshold_as_of'])
    index = frame.set_index(KEYS, verify_integrity=True)
    expected = {(model, seed, path_id) for model, size in completed.items()
                for seed in settings['seeds'] for path_id in range(size)}
    if set(index.index) != expected:
        raise ValueError('account identities differ from the complete checkpoint prefixes')
    latent_by_id = {(row['model'], row['seed'], row['path_id']): row['events'] for row in path_evidence}
    if len(latent_by_id) != len(path_evidence) or set(latent_by_id) != {
            key for key in expected if key[0] in generators}:
        raise ValueError('latent evidence differs from the complete model account identities')
    digests = index['price_sha256'].to_dict() if expected else {}
    matches = 0
    serial_accounts = []
    with TemporaryDirectory(prefix='factorlab-mc05-f03-serial-') as temporary:
        replay = partial(_replay_account, start=definition['start'], config=config, rules=rules,
            capacity=definition['capacity'], temporary=temporary, include_path_evidence=True,
            provenance=capture_provenance(source_context_root()), diagnostic=capture_account_diagnostics)
        for model, size in completed.items():
            for seed in settings['seeds']:
                for tier in (tier for tier in settings['sizes'] if tier <= size):
                    for offset, paths, _ in _generator_batch(model, inputs, generators, seed, 0,
                                                            tier, settings, definition):
                        for path_id, prices in enumerate(paths, offset):
                            key = model, seed, path_id
                            if digests[key] != price_digest(prices):
                                raise ValueError(f'prefix price differs at {key} tier {tier}')
                            matches += 1
                            if (seed, path_id, tier) != (definition['resources']['pilot_seed'], 0, settings['sizes'][0]):
                                continue
                            generator = generators.get(model)
                            latent = (generator.path_evidence(PathPlan(seed=seed, n_paths=tier,
                                n_steps=settings['steps'], batch_size=settings['batch_size']), path_id)
                                if generator is not None else None)
                            metrics, events, *_ = replay(prices, latent)
                            parallel = index.loc[key]
                            for name, value in metrics.items():
                                if name != 'temporary_account_bytes' and not (parallel[name] == value or
                                        value is None and pd.isna(parallel[name])):
                                    raise ValueError(f'serial/spawn difference in {key}: {name}')
                            if events != latent_by_id.get(key):
                                raise ValueError(f'serial/spawn latent evidence differs at {key}')
                            serial_accounts.append(dict(zip(KEYS, key)))

    first_tier = settings['sizes'][0]
    prefix = index[index.index.get_level_values('path_id') < first_tier]
    frozen = pd.read_csv(FROZEN_RUN/'paths.csv', float_precision='round_trip').set_index(KEYS)
    common = prefix.index.intersection(frozen.index)
    left, right = prefix.reindex(common), frozen.reindex(common)
    fields = [key for key in right if key not in NON_ECONOMIC + CORRELATIONS]
    differences = [key for key in fields if not
        ((left[key] == right[key]) | (left[key].isna() & right[key].isna())).all()]
    correlations = {key: dict(changed=int((left[key] != right[key]).sum()),
                         maximum_absolute_delta=float((left[key] - right[key]).abs().max()))
                    for key in CORRELATIONS if len(common)}
    return dict(
        unique_accounts=len(index), price_prefix_digest_comparisons=matches,
        distinct_price_digests=len(set(digests.values())),
        serial_parallel_exact_accounts=serial_accounts,
        f02_prefix_comparison=dict(reused=0, rows=len(common), compared_fields=len(fields),
            field_differences=differences, separately_compared_correlations=correlations,
            reason='Frozen summaries do not retain a close-series identity and complete ledger for every account.'),
        maximum_absolute_residual={key: float(index[key].abs().max()) if len(index) else None
            for key in ('reconciliation_residual', 'nav_residual', 'inventory_residual')},
    )


def _execution_report(definition, actual, calibration, audit, completed, coverage, decisions, failure, outcome):
    d = definition['dataset']
    settings = definition['experiment']
    lines = ['# MC-05-F03 执行', '', ' '.join(outcome.values()), '',
        f"NVDAUSDT，{d['source']}，1,000 USDT 初始资金，168 小时收盘模拟。",
        f"数据版本 `{d['data_version']}`，bars SHA-256 `{d['bars_sha256']}`。",
        f"历史观察 {d['observations'][0]} 至 {d['observations'][1]}，{d['returns']} 条小时收益。",
        f"快照采集 {d['acquired_at']}，校准截止 {d['as_of']}，模拟起点 {definition['start']}。",
        'Jump-down 及 Regime historical 均为冻结 MC-03 v1；Moving-block bootstrap 用冻结 CPU MT19937、24 小时块。',
        'Grid 沿用 F02 几何网格、0 初始库存、4 bps 手续费、6 bps 滑点及 0.10 合成成交容量。Funding 为 NOT_SIMULATED。',
        f"种子 {settings['seeds']}，batch {settings['batch_size']}，预定档位 {settings['sizes']}，8 workers。",
        '', '| 模型 | 已完成档位 | 账户 | 成交 | 参与率 |',
        '| --- | --- | ---: | ---: | ---: |']
    for model, size in completed.items():
        accounts, traded = coverage[model]
        sizes = ', '.join(str(tier) for tier in settings['sizes'] if tier <= size)
        rate = f'{traded/accounts:.2%}' if accounts else '—'
        lines.append(f'| {model} | {sizes or "—"} | {accounts} | {traded} | {rate} |')
    lines.extend(['', '| 阶段 | 墙钟 s | 全树 CPU s | 采样 RSS 峰值 MiB | 临时文件峰值 MiB |',
        '| --- | ---: | ---: | ---: | ---: |'])
    for label, measurement in [('预校准', calibration['performance']), ('正式运行', actual)]:
        lines.append(f"| {label} | {measurement['wall_seconds']:.3f} | {measurement['tree_cpu_seconds']:.3f} | "
                     f"{measurement['peak_tree_rss_bytes']/2**20:.2f} | {measurement['peak_temporary_disk_bytes']/2**20:.2f} |")
    lines.extend(['',
        '整组计时包含生成、回放、账户清理和组写出。首个组也承担 spawn 工人的冷启动。',
        f"预校准在首组进入前耗时 {calibration['performance']['startup_wall_seconds']:.3f} 秒；"
        f"CLI 解释器内准备和快照装载耗时 {calibration['cli_preparation_wall_seconds']:.3f} 秒。",
        f"正式预算 {settings['budget_seconds']} 秒，安全系数 {settings['cost_safety_factor']}，保留 "
        f"{definition['resources']['output_reserve_seconds']} 秒写出储备。RSS 与文件长度以约 100 ms 采样，"
        'RSS 合计可能计入共享页，采样间峰值可能遗漏。',
        '', '| 模型 | 目标档位 | 新账户 | 入组前 s | 安全后预计 s | 实际组 s | 准入或拒绝原因 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | --- |'])
    group_lookup = {(g['model'], g['size']): g for g in actual['groups']}
    for decision in decisions:
        model, size = decision['model'], decision['size']
        group = group_lookup.get((model, size), {})
        reason = '准入' if decision['admitted'] else ', '.join(decision['reasons'])
        fields = [decision.get('elapsed_seconds'), decision.get('projected_additional_seconds'),
                  group.get('wall_seconds')]
        timings = ' | '.join('—' if seconds is None else f'{seconds:.3f}' for seconds in fields)
        lines.append(f"| {model} | {size} | {decision['additional_accounts']} | {timings} | {reason} |")
    frozen = audit['f02_prefix_comparison']
    lines.extend(['', f"异常及中断记录：{failure if failure else '正式调度未记录异常或中断'}。",
        f"共 {audit['unique_accounts']} 个唯一账户身份，完成 {audit['price_prefix_digest_comparisons']} 次逐档价格指纹比较，"
        f"串行与 spawn 经济及路径证据逐项对照 {len(audit['serial_parallel_exact_accounts'])} 个固定账户。",
        f"FIFO、导出 NAV 和库存残差绝对峰值为 {list(audit['maximum_absolute_residual'].values())}。"
        'FIFO 归因逐成交核对 Paper 权威成交事件、费用和滑点。',
        f"F02 复用账户 0。跨次运行比较 {frozen['rows']} 个首档账户、每账户 {frozen['compared_fields']} 列，"
        f"差异列：{', '.join(frozen['field_differences']) or '无'}。"
        '价格一阶自相关和平方收益自相关单列比较，相关系数不进入风险估计。'
        'F02 缺少每条路径完整价格身份和逐笔原始账户证据，F03 使用本轮回放和审计。',
        '', '运行定义、校准、资源、准入、账户身份和审计收据随报告保存。'])
    return '\n'.join(lines) + '\n'


def report(output, *, calibration, inputs, receipt, config, rules, definition):
    output = Path(output)
    settings = definition['experiment']
    models = definition['models']
    checkpoints = _read(output/'groups.json') if (output/'groups.json').exists() else []
    completed = _checkpoint_counts(checkpoints, settings, models)
    paths = (pd.read_csv(output/'paths.csv', float_precision='round_trip') if (output/'paths.csv').exists()
             else pd.DataFrame(columns=KEYS))
    frame = paths[paths['path_id'] < paths['model'].map(completed)]
    path_file = output/'path-evidence.json'
    events = [row for row in _read(path_file) if row['path_id'] < completed[row['model']]] if path_file.exists() else []
    audit = _audit_paths(frame, events, inputs=inputs, receipt=receipt, config=config, rules=rules,
                         definition=definition, completed=completed)
    rows = frame.to_dict('records')
    result = analyze(rows, completed_by_model=completed, models=models, seeds=settings['seeds'],
                     sizes=settings['sizes'], resamples=settings['resamples'])
    failure_file = output/'failure.json'
    failure = _read(failure_file) if failure_file.exists() else None
    decisions = _read(output/'decisions.json')
    actual = _read(output/'resources.json')
    accepted = (not failure and (output/'summary.json').exists()
                and all(completed.values()) and all(v == 0 for v in audit['maximum_absolute_residual'].values())
                and actual['wall_seconds'] <= settings['budget_seconds'])
    outcome = dict(engineering_status='ENGINEERING_ACCEPTED' if accepted else 'ENGINEERING_BLOCKED',
                   statistical_status=result['statistical_status'], convergence_status=result['convergence_status'],
                   financial_status='FINANCIAL_NOT_ESTABLISHED', execution_authorization='NO LIVE MONEY')
    coverage = {}
    for model in models:
        sample = [row for row in rows if row['model'] == model]
        coverage[model] = len(sample), sum(row['traded'] for row in sample)
    write(output/'audit.json', audit | outcome | dict(completed_by_model=completed))
    write(output/'statistics.json', result)
    (output/'REPORT.md').write_text(_execution_report(definition, actual, calibration, audit,
        completed, coverage, decisions, failure, outcome), encoding='utf-8')
    (output/'STATISTICS.md').write_text(render_report(result), encoding='utf-8')
    (output/'FINANCIAL.md').write_text(render_financial_report(rows, events,
        historical_returns=inputs.historical_returns[:, 0], calibration=calibration,
        steps=settings['steps']), encoding='utf-8')
