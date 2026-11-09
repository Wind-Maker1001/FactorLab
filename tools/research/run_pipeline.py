"""Run and verify a configuration-pinned SYNTHETIC / SOFTWARE_ONLY experiment."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

from factorlab.application.research_pipeline import BOUNDARY, ResearchPipeline
from factorlab.dataset.identity import file_hash
from factorlab.dataset.partitioned import _require_psutil
from factorlab.experiments import ExperimentTracker
from factorlab.experiments.provenance import capture_provenance
from tools.research.pipeline_config import canonical, configure, identity, load_config

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = 'SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def summarize(result, config):
    models = {}
    for name, walk in result.walk_forward.items():
        execution = result.executions[name]
        prediction, portfolio = result.evaluations[name]
        models[name] = dict(folds=len(walk.fold_summary), predictions=len(walk.predictions),
            prediction_metrics={key: asdict(value) for key, value in prediction.metrics.items()},
            portfolio_metrics={key: asdict(value) for key, value in portfolio.metrics.items()},
            final_nav=float(execution.nav.iloc[-1].strategy_nav),
            transaction_cost_cny=float(execution.nav.transaction_cost.sum()),
            trades=len(execution.trades), rejections=len(execution.rejections))
    return dict(evidence=EVIDENCE, boundary=BOUNDARY, dataset_identity=result.dataset.identity_hash,
                execution_mode=config.execution_mode, objective=config.optimizer.objective,
                run_ids=list(result.run_ids), models=models)


def chinese_summary(summary):
    lines = ['# 配置驱动合成研究摘要', '', EVIDENCE, '', BOUNDARY, '',
        '以下仅描述固定合成样本；不是实际市场回测业绩，不用于选择或推荐 Test 表现最好的模型。', '',
        f"执行模式：{summary['execution_mode']}；组合目标：{summary['objective']}。",
        '预测分数仅是横截面偏好，不是预期收益率。minimum_variance 只使用风险协方差，因此不同预测可能得到相同组合。', '',
        '| 模型 | 折数 | OOS 行数 | 预测 RankIC | 合成组合净收益 | 交易成本（元） | 成交笔数 | 拒单数 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, model in summary['models'].items():
        ic = model['prediction_metrics']['prediction_rank_ic_mean']
        ic_text = f"{ic['value']:.6f}" if ic['value'] is not None else ic['status']
        lines.append(f"| {name} | {model['folds']} | {model['predictions']} | {ic_text} | {(model['final_nav']-1):.4%} | {model['transaction_cost_cny']:.2f} | {model['trades']} | {model['rejections']} |")
    lines += ['', '预测能力：RankIC 为描述性 OOS 指标，不等同组合收益或真实 Alpha。',
        '组合收益：由实际模拟成交、现金和持仓账本计算；目标仓位不等于实际持仓。净值及风险评价见各模型 evaluation.json。',
        '交易成本：包含配置的佣金、冲击（既有引擎的滑点费用代理）及既有日期生效税费；并非真实历史可成交报价证明。换手、拒单及目标偏离见执行账本。',
        '统计显著性：NOT_DETERMINED；本工作流没有进行预声明的负对照、多重检验和统计推断，正收益本身不是显著性证据。负对照正收益也不构成软件测试失败。',
        '合成数据限制：固定种子、24 证券、合成工作日日历，不验证真实市场 PIT、HFQ 或金融准入。',
        '复现：使用 normalized-config.json 和 reproduction.json 中的命令，指定新的输出目录。', '']
    return '\n'.join(lines)


def run(config_path, output):
    started, cpu = time.perf_counter(), time.process_time()
    document = load_config(config_path)
    psutil = _require_psutil()
    output = Path(output).resolve()
    config, hub, data_identity = configure(document, output)
    # Exclusive directory creation reserves the experiment before any artifacts.
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'normalized-config.json', document)
    command = [sys.executable, '-m', 'tools.research.run_pipeline', '--config',
               str(output / 'normalized-config.json'), '--output', '<fresh-output>']
    tracker = ExperimentTracker(output / 'experiments')
    provenance = capture_provenance(ROOT)
    write_json(output / 'identity.json', dict(config_sha256=identity(document),
        data=data_identity, factors=[asdict(ref) for ref in config.builder.factor_refs],
        source_files={str(path.relative_to(ROOT)): file_hash(path) for path in
            [Path(__file__), Path(__file__).with_name('pipeline_config.py'),
             ROOT / 'examples/research/research_pipeline_synthetic.py',
             ROOT / 'src/factorlab/exploratory/factor_discovery/synthetic.py']}, **provenance))
    result = ResearchPipeline(config).run(hub, tracker=tracker, repo_root=ROOT, command=command)
    summary = summarize(result, config)
    write_json(output / 'summary.json', summary)
    (output / 'SUMMARY.zh-CN.md').write_text(chinese_summary(summary), encoding='utf-8')
    write_json(output / 'reproduction.json', dict(command=command, cwd='repository root',
        environment='Use the exact Python/package versions in identity.json; set PYTHONPATH=src.',
        config_sha256=identity(document), dataset_identity=result.dataset.identity_hash))
    write_json(output / 'resources.json', dict(wall_seconds=time.perf_counter()-started,
        cpu_seconds=time.process_time()-cpu, final_rss_bytes=psutil.Process().memory_info().rss,
        rss_measurement='final process RSS; not peak or model-exclusive memory',
        timing_scope='configuration, data construction, four-model research and reporting in one process'))
    artifacts = {str(path.relative_to(output)): dict(path=str(path), sha256=file_hash(path))
                 for path in output.rglob('*') if path.is_file() and
                 'dataset' not in path.relative_to(output).parts and path.suffix != '.sqlite3'}
    workflow_id = document['experiment_id'] + '-workflow'
    first = tracker.reproduction(result.run_ids[0])['definition']
    definition = dict(first)
    definition['model'] = 'configured-research-workflow-v1'
    definition['parameters'] = dict(config_sha256=identity(document), configuration=document,
                                    data_identity=data_identity, run_ids=list(result.run_ids))
    tracker.create_experiment(workflow_id, definition)
    tracker.append_run(workflow_id, workflow_id, **provenance, command=command,
        status='SUCCEEDED', qualification='SYNTHETIC', artifacts=artifacts,
        validation=dict(config_sha256=identity(document), dataset_identity=result.dataset.identity_hash))
    manifest = dict(schema_version=1, workflow_run_id=workflow_id, config_sha256=identity(document),
        dataset_identity=result.dataset.identity_hash, model_run_ids=list(result.run_ids),
        files={str(path.relative_to(output)): file_hash(path) for path in output.rglob('*')
               if path.is_file() and 'dataset' not in path.relative_to(output).parts and path.suffix != '.sqlite3'})
    write_json(output / 'receipt.json', manifest)
    verify(output)
    print(chinese_summary(summary))
    return result


def verify(output):
    """Check retained bytes, immutable Tracker envelopes and configuration bindings."""
    output = Path(output).resolve()
    manifest = json.loads((output / 'receipt.json').read_text(encoding='utf-8'))
    document = load_config(output / 'normalized-config.json')
    if identity(document) != manifest['config_sha256']:
        raise ValueError('configuration identity mismatch')
    for name, expected in manifest['files'].items():
        path = (output / name).resolve()
        if not path.is_relative_to(output) or not path.is_file() or file_hash(path) != expected:
            raise ValueError(f'missing or changed output artifact: {name}')
    tracker = ExperimentTracker(output / 'experiments')
    receipt = tracker.reproduction(manifest['workflow_run_id'], verify_artifacts=True)
    parameters = receipt['definition']['parameters']
    if parameters['config_sha256'] != identity(document) or parameters['configuration'] != document:
        raise ValueError('Tracker configuration identity mismatch')
    if parameters['run_ids'] != manifest['model_run_ids'] or len(manifest['model_run_ids']) != 4:
        raise ValueError('model run identity mismatch')
    required = {'normalized-config.json', 'identity.json', 'summary.json', 'SUMMARY.zh-CN.md',
                'reproduction.json', 'resources.json', 'comparison.csv'}
    if not required.issubset(manifest['files']):
        raise ValueError('incomplete output receipt')
    stored_identity = json.loads((output / 'identity.json').read_text(encoding='utf-8'))
    if stored_identity['data'] != parameters['data_identity']:
        raise ValueError('data identity mismatch')
    for run_id in manifest['model_run_ids']:
        model = tracker.reproduction(run_id, verify_artifacts=True)
        if model['definition']['dataset']['sha256'] != manifest['dataset_identity']:
            raise ValueError('dataset identity mismatch')
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    if (output / 'SUMMARY.zh-CN.md').read_text(encoding='utf-8') != chinese_summary(summary):
        raise ValueError('Chinese summary differs from result receipt')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=EVIDENCE + ' / ' + BOUNDARY)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args(argv)
    if args.verify:
        verify(args.output)
        print('完整性验证通过：' + EVIDENCE)
    else:
        if args.config is None:
            parser.error('--config is required for execution')
        run(args.config, args.output)


if __name__ == '__main__':
    main()
