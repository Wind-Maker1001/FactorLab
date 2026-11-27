"""Publish compact measured evidence and Markdown tables from completed studies."""
import argparse
import json
from pathlib import Path
import statistics

from factorlab.experiments.mc_colab_benchmark import compare_studies, performance_comparison, write_json


def publish(source, output):
    source,output=Path(source),Path(output)
    output.mkdir(parents=True,exist_ok=False)
    studies={}
    for exclusion in sorted(source.glob('*-exclusion.json')):
        write_json(output/exclusion.name,json.loads(exclusion.read_text(encoding='utf-8')))
    for directory in sorted(source.iterdir()):
        if directory.name.startswith('excluded-'):
            continue
        if not (directory/'study.json').is_file():
            continue
        study=json.loads((directory/'study.json').read_text(encoding='utf-8'))
        studies[directory.name]=study
        destination=output/directory.name
        destination.mkdir()
        for name in ('study.json','configuration.json','provenance.json'):
            path=directory/name
            if path.exists():
                value=json.loads(path.read_text(encoding='utf-8'))
                if name=='provenance.json':
                    value.pop('command',None)
                    value['source_snapshot']='retained in original local experiment directory; not bundled'
                write_json(destination/name,value)
        for path in sorted(directory.glob('wave-*.json')):
            wave=json.loads(path.read_text(encoding='utf-8'))
            for outcome in wave['outcomes']:
                outcome.pop('job',None)
                outcome.pop('rows',None)  # Path-level aggregate records are already in study.json.
            write_json(destination/path.name,wave)
    comparisons={}
    for suffix in ('reliability','scale','crossover'):
        a,b=studies['cpu-'+suffix],studies['gpu-'+suffix]
        comparisons[suffix]=dict(statistics=compare_studies(a,b),performance=performance_comparison(a,b))
    write_json(output/'comparisons.json',comparisons)
    lines=['# Local measured results','',
        'Hardware: i7-14700KF (single process, BLAS threads=1) and RTX 2070 8GB. Float64 throughout.',
        'Times include exact engine summary, exclude separately recorded external interval analysis. Rows with one seed are performance/capacity evidence, not formal statistical acceptance.','',
        '## Matched steady performance','',
        '| Study / workload | Paths | Assets | Steps | CPU s | GPU s | Speedup | Cold CPU / GPU s |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for suffix,comparison in comparisons.items():
        for row in comparison['performance']:
            c=row['cell']
            lines.append(f"| {suffix}/{c['name']} | {c['n_paths']} | {c['assets']} | {c['n_steps']} | {row['cpu_median_seconds']:.6f} | {row['gpu_median_seconds']:.6f} | {row['observed_speedup']:.2f} | {row['cold_cpu_median_seconds']:.3f} / {row['cold_gpu_median_seconds']:.3f} |")
    lines+=['','## Five-seed risk differences','',
        'GPU minus CPU; simultaneous 95% Welch/Bonferroni intervals across nine metrics within each workload. Seed-level Student-t normal approximation; no business equivalence margin.','',
        '| Workload | Metric | Difference | Lower | Upper |','|---|---|---:|---:|---:|']
    for row in comparisons['reliability']['statistics']:
        for name,contrast in row['contrasts'].items():
            lo,hi=contrast['family_interval_95']
            lines.append(f"| {row['cell']['name']} | {name} | {contrast['gpu_minus_cpu']:.7f} | {lo:.7f} | {hi:.7f} |")
    lines+=['','## Seed means and variation','',
        'Full per-seed path-level confidence intervals are retained in each study.json (`intervals`).','',
        '| Device / workload | Metric | Mean | Seed SD | Min | Max |','|---|---|---:|---:|---:|---:|']
    for key in ('cpu-reliability','gpu-reliability'):
        for group in studies[key]['independent_seed_summary']:
            for metric,v in group['metrics'].items():
                lines.append(f"| {group['device']}/{group['cell']['name']} | {metric} | {v['mean']:.7f} | {v['seed_sd']:.7f} | {v['minimum']:.7f} | {v['maximum']:.7f} |")
    lines+=['','## Resource and analysis cost','',
        '| Study | Sampled worker RSS peak MiB | Whole-device peak MiB | External statistics total s | Study wall s |',
        '|---|---:|---:|---:|---:|']
    for key,study in studies.items():
        rows=study['rows']
        rss=max((r['resources']['rss_peak_bytes'] for r in rows),default=0)/1024**2
        gpu=max((r['resources']['device_used_peak_bytes'] or 0 for r in rows),default=0)/1024**2
        lines.append(f"| {key} | {rss:.1f} | {gpu:.1f} | {sum(r['external_statistics_seconds'] for r in rows):.3f} | {study['elapsed_seconds']:.3f} |")
    lines+=['','## Synchronized phases (profiling only)','',
        '| Device / workload | Phase | Warm median s |','|---|---|---:|']
    for key in ('cpu-profile','gpu-profile'):
        cells={r['cell']['name'] for r in studies[key]['rows']}
        for cell in sorted(cells):
            rows=[r for r in studies[key]['rows'] if r['cell']['name']==cell and r['repeat']>0]
            for phase in rows[0]['engine']['phase_seconds']:
                seconds=statistics.median(r['engine']['phase_seconds'][phase] for r in rows)
                lines.append(f'| {key}/{cell} | {phase} | {seconds:.7f} |')
    lines+=['','## CPU multiprocess aggregate','',
        'Independent complete jobs; includes startup and repeated result analysis, not just simulation kernel time. Child-inclusive RSS is sampled and CPU seconds cover measured runner regions.','',
        '| Wave | Jobs | Completed paths | Wall s | Aggregate paths/s | Child-inclusive RSS MiB |',
        '|---|---:|---:|---:|---:|---:|']
    for path in sorted((source/'cpu-parallel').glob('wave-*.json')):
        wave=json.loads(path.read_text())
        lines.append(f"| {path.stem} | {len(wave['outcomes'])} | {wave['completed_paths']} | {wave['wall_seconds']:.3f} | {wave['completed_paths']/wave['wall_seconds']:.1f} | {wave['child_inclusive_memory']['rss_peak_bytes']/1024**2:.1f} |")
    if 'gpu-large' in studies:
        lines+=['','## Large-path capacity (one seed, no path bootstrap)','',
            '| Paths | Repeat | Runner s | Paths/s | External exact metrics s | RSS MiB |',
            '|---:|---:|---:|---:|---:|---:|']
        for r in studies['gpu-large']['rows']:
            lines.append(f"| {r['cell']['n_paths']} | {r['repeat']} | {r['runner_wall_seconds']:.3f} | {r['paths_per_second']:.1f} | {r['external_statistics_seconds']:.3f} | {r['resources']['rss_peak_bytes']/1024**2:.1f} |")
    (output/'MEASURED.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    publish(args.source,args.output)
