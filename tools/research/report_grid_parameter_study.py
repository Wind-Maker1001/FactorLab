"""Static, reproducible MC-GRID-OPT-01 result tables and sensitivity charts."""
import argparse
import csv
import json
from pathlib import Path
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


METRICS = (
    ('mean_net_return', 'Mean net return', ('net_return', 'mean')),
    ('cvar95', 'Loss CVaR 95%', ('cvar95',)),
    ('cvar99', 'Loss CVaR 99%', ('cvar99',)),
    ('drawdown_q99', 'Maximum drawdown · q99', ('maximum_drawdown', 'quantiles', '0.99')),
    ('fill_probability', 'Fill probability', ('fill_probability',)),
    ('efficiency', 'Profit / mean committed capital', ('profit_per_mean_committed',)),
)
BLUE = '#2563eb'


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def flatten(value, prefix=''):
    result = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(flatten(item, name))
        else:
            result[name] = compact(item) if isinstance(item, list) else item
    return result


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def metric_values(summary):
    result = {}
    for name, _, keys in METRICS:
        if name == 'efficiency':
            result[name] = summary.get('profit_per_mean_committed')
            continue
        value = summary
        for key in keys:
            value = value[key]
        result[name] = value
    return result


def average(rows):
    return {name: None if any(row[name] is None for row in rows)
            else sum(row[name] for row in rows) / len(rows)
            for name, _, _ in METRICS}


def safe(value):
    return re.sub(r'[^a-zA-Z0-9_-]', '-', value)


def style(ax):
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#e5e7eb', linewidth=.6)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('bottom', 'left'):
        ax.spines[side].set_color('#d1d5db')
    ax.tick_params(labelsize=9, color='#d1d5db')
    ax.margins(x=.09)


def save(fig, output, stem):
    fig.savefig(output / f'{stem}.svg', metadata={'Date': None})
    fig.savefig(output / f'{stem}.png', dpi=120, metadata={'Software': 'grid parameter report'})
    plt.close(fig)
    return f'![{stem}]({stem}.svg) · [PNG]({stem}.png)'


def sensitivity_charts(rows, output):
    links = []
    groups = sorted({(row['model'], row['axis']) for row in rows if row['axis'] != 'baseline'})
    for model, axis in groups:
        group = [row for row in rows if row['model'] == model and row['axis'] == axis]
        numeric = all(isinstance(row['_value'], (int, float)) and not isinstance(row['_value'], bool)
                      for row in group)
        if numeric:
            group.sort(key=lambda row: row['_value'])
        xs = [row['_value'] for row in group] if numeric else list(range(len(group)))
        fig, axes = plt.subplots(2, 3, figsize=(15, 8), layout='constrained')
        fig.suptitle(f'A · {model} · {axis}\nOne-factor sensitivity; other parameters fixed', fontsize=14)
        for ax, (name, title, _) in zip(axes.flat, METRICS):
            ys = [float('nan') if row[name] is None else row[name] for row in group]
            if any(row[name] is not None for row in group):
                ax.plot(xs, ys, linestyle='-' if numeric else 'None', linewidth=1.5,
                        marker='o', markersize=6, markeredgecolor='white', markeredgewidth=1.5, color=BLUE)
            else:
                ax.text(.5, .5, 'Undefined: no committed capital', transform=ax.transAxes,
                        ha='center', fontsize=10)
            if not numeric:
                ax.set_xticks(xs, [str(row['_value']) if not isinstance(row['_value'], (dict, list))
                                  else compact(row['_value']) for row in group], rotation=45, ha='right')
            if name != 'efficiency':
                ax.yaxis.set_major_formatter(PercentFormatter(1))
            ax.set_title(title, loc='left', fontsize=11)
            ax.set_xlabel(axis, fontsize=9)
            style(ax)
        links.append(save(fig, output, f'A-sensitivity-{safe(model)}-{safe(axis)}'))
    return links


def pareto_charts(stage, rows, output):
    links, table = [], []
    for model in sorted({row['model'] for row in rows}):
        group = [row for row in rows if row['model'] == model and row['role'] == 'candidate']
        if not group:
            continue
        front = [row for row in group if not any(
            other['mean_net_return'] >= row['mean_net_return'] and other['cvar99'] <= row['cvar99']
            and (other['mean_net_return'] > row['mean_net_return'] or other['cvar99'] < row['cvar99'])
            for other in group)]
        for row in group:
            table.append({**row, 'pareto_2d': row in front})
        fig, ax = plt.subplots(figsize=(8, 6), layout='constrained')
        ax.scatter([r['cvar99'] for r in group], [r['mean_net_return'] for r in group],
                   color='#6b7280', s=40, edgecolors='white', linewidths=1.5, label='Other candidates')
        ax.scatter([r['cvar99'] for r in front], [r['mean_net_return'] for r in front],
                   color=BLUE, marker='D', s=48, edgecolors='white', linewidths=1.5,
                   label='2-objective non-dominated')
        ax.set(title=f'{stage} · {model}\nCandidate return / loss-tail trade-off',
               xlabel='Loss CVaR 99% (lower is better)', ylabel='Mean net return (higher is better)')
        ax.xaxis.set_major_formatter(PercentFormatter(1))
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        style(ax)
        ax.legend(fontsize=9)
        links.append(save(fig, output, f'{stage}-pareto-{safe(model)}'))
    write_csv(output / f'{stage}-pareto.csv', table)
    return links


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--a', required=True, type=Path)
    for stage in 'bcd':
        parser.add_argument(f'--{stage}', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    plt.rcParams.update({'svg.hashsalt': 'grid-parameter-study', 'font.family': 'DejaVu Sans',
                         'text.color': '#111827', 'axes.labelcolor': '#111827'})
    report = ['# Grid parameter study', '',
              'NO LIVE MONEY. Curves describe the supplied simulation results, not a unique optimum.',
              'Returns, drawdowns, loss CVaRs and fill probabilities use fractional units in CSV and percent in charts. '
              'Negative loss CVaR is retained. Efficiency is profit divided by mean committed capital; '
              'undefined efficiency is an empty CSV field, never zero.', '',
              'Each chart has one model. Replicate statistics receive equal weight within each model; '
              'quantiles and CVaRs are averages of cell statistics, not pooled-path statistics. '
              'Models are never pooled. Lines connect numeric one-factor levels, not time or fitted responses; '
              'composite and categorical levels use unconnected dots. Complete IDs and values are in CSV.', '']
    sensitivity = []
    for stage in 'ABCD':
        root = getattr(args, stage.lower())
        if root is None:
            continue
        root = root.resolve()
        definition = json.loads((root / 'definition.json').read_text(encoding='utf-8'))
        if definition['stage'] != stage:
            raise ValueError(f'{root}: expected stage {stage}')
        paths = sorted(root.glob('*/status.json'))
        cells = [json.loads(path.read_text(encoding='utf-8')) for path in paths]
        expected = {(m, r) for m in definition['settings']['models']
                    for r in range(definition['settings'][stage]['replicates'])}
        if (len(cells) != len(expected) or any(c['status'] != 'COMPLETE' for c in cells)
                or {(c['model'], c['replicate']) for c in cells} != expected):
            raise ValueError(f'{root}: stage cells are incomplete')
        (args.output / f'{stage}-definition.json').write_text(
            json.dumps(definition, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        configs = definition['configurations']
        write_csv(args.output / f'{stage}-configurations.csv',
                  [{'stage': stage, 'config_id': key, **flatten(config)} for key, config in sorted(configs.items())])
        write_csv(args.output / f'{stage}-one-factor-axes.csv', definition['one_factor_axes'])
        write_csv(args.output / f'{stage}-rejected.csv', definition['rejected'])
        full, paired, grouped, inventory = [], [], {}, []
        for path, cell in zip(paths, cells):
            identity = {key: cell[key] for key in ('stage', 'model', 'replicate', 'seed', 'paths')}
            inventory.append({**identity, 'status_file': str(path), 'bytes': path.stat().st_size})
            for key, summary in sorted(cell['summaries'].items()):
                values = metric_values(summary)
                parent = key.removesuffix('-hold').removesuffix('-endowed')
                full.append({**identity, 'config_id': key,
                             'configuration_ref': parent if parent in configs else None,
                             **flatten(summary)})
                grouped.setdefault((cell['model'], key), []).append(values)
                array = path.parent / f'{key}.npy'
                inventory.append({**identity, 'config_id': key, 'array_file': str(array),
                                  'bytes': array.stat().st_size})
            for comparison in ('paired_vs_baseline', 'paired_vs_controls'):
                for key, value in cell[comparison].items():
                    paired.append({**identity, 'config_id': key, 'comparison': comparison, **flatten(value)})
        aggregate = []
        for (model, key), values in sorted(grouped.items()):
            role = ('baseline' if key in ('baseline', 'arithmetic-baseline') else
                    'candidate' if key in configs else 'control')
            aggregate.append({'stage': stage, 'model': model, 'config_id': key, 'role': role,
                              'replicates': len(values), **average(values)})
        for name, rows in (('cell-summaries', full), ('paired-results', paired),
                           ('model-summaries', aggregate), ('inputs', inventory)):
            write_csv(args.output / f'{stage}-{name}.csv', rows)
        size = sum(row['bytes'] for row in inventory) + (root / 'definition.json').stat().st_size
        report += [f'## Stage {stage}', '', f'Input: `{root}`. {len(cells)} complete cells; '
                   f'{len(configs)} configurations (including baselines); '
                   f'{sum(c["paths"] for c in cells):,} scenario paths across cells; '
                   f'{len(full):,} configuration/control summaries; {size:,} bytes in definition, statuses and arrays.', '',
                   ' · '.join(f'[{name}]({stage}-{name}.csv)' for name in
                              ('configurations', 'cell-summaries', 'paired-results', 'model-summaries',
                               'one-factor-axes', 'rejected', 'inputs')) +
                   f' · [Frozen definition]({stage}-definition.json)', '']
        if stage == 'A':
            lookup = {(r['model'], r['config_id']): r for r in aggregate}
            for axis in definition['one_factor_axes']:
                for model in sorted(definition['settings']['models']):
                    row = lookup.get((model, axis['id']))
                    if row is not None:
                        sensitivity.append({**row, 'axis': axis['axis'], 'axis_value': compact(axis['value']),
                                            'efficiency_status': 'undefined' if row['efficiency'] is None else 'defined',
                                            '_value': axis['value']})
            write_csv(args.output / 'A-sensitivity.csv',
                      [{k: v for k, v in row.items() if k != '_value'} for row in sensitivity])
            report += ['[Sensitivity table](A-sensitivity.csv)', *sensitivity_charts(sensitivity, args.output), '']
        else:
            report += [f'[Pareto coordinates and IDs]({stage}-pareto.csv)',
                       'Diamonds identify the two-objective front within this model, excluding controls and baselines. '
                       'This is not the study’s higher-dimensional selection rule and imposes no scalar rank.',
                       *pareto_charts(stage, aggregate, args.output), '']
        if stage == 'D':
            assessment = json.loads((root / 'assessment.json').read_text(encoding='utf-8'))
            (args.output / 'D-assessment.json').write_text(json.dumps(assessment, indent=2) + '\n', encoding='utf-8')
            intervals = []
            for config_id, configuration in assessment['configurations'].items():
                for model, model_result in configuration['models'].items():
                    for family in ('metrics', 'risk_metrics'):
                        for metric, interval in model_result[family].items():
                            intervals.append({'config_id': config_id, 'model': model,
                                              'metric': metric, **interval})
            write_csv(args.output / 'D-seed-intervals.csv', intervals)
            for model in sorted(definition['settings']['models']):
                rows = [r for r in intervals if r['model'] == model and r['metric'] == 'net_return']
                fig, ax = plt.subplots(figsize=(10, 7), layout='constrained')
                for index, row in enumerate(rows):
                    ax.plot([row['lower'], row['upper']], [index, index], color=BLUE, linewidth=2)
                    ax.scatter([row['mean']], [index], color=BLUE, s=40)
                ax.set_yticks(range(len(rows)), [r['config_id'] for r in rows], fontsize=7)
                ax.axvline(0, color='#6b7280', linewidth=1)
                ax.set(title=f'D · {model} · mean net return, 95% seed intervals\n5 independent seeds × 100,000 paths; descriptive, not simultaneous',
                       xlabel='Net return', ylabel='Locked configuration')
                ax.xaxis.set_major_formatter(PercentFormatter(1))
                style(ax)
                report.append(save(fig, args.output, f'D-seed-intervals-{safe(model)}'))
            report += ['[Seed uncertainty table](D-seed-intervals.csv)', '### Independent validation assessment', '',
                       f'Recorded research shortlist: {len(assessment["final_ids"])} configurations. '
                       f'IDs: {compact(assessment["final_ids"])}.',
                       assessment['candidate_rule'], assessment['interval_method'],
                       assessment['stress_role'], assessment['shortlist_meaning'],
                       *assessment['interpretation'], '[Full recorded assessment](D-assessment.json)', '']
    (args.output / 'report.md').write_text('\n\n'.join(report) + '\n', encoding='utf-8')
    print(args.output / 'report.md')


if __name__ == '__main__':
    main()
