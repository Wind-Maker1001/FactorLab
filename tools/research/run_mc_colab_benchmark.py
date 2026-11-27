"""Run MC-COLAB-02 locally or in a Colab terminal with an identical configuration."""
import argparse
import json
from pathlib import Path

from factorlab.experiments.mc_colab_benchmark import (
    compare_studies, cu_cost, performance_comparison, run_study, write_json,
)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run')
    run.add_argument('--config',required=True)
    run.add_argument('--output',required=True)
    run.add_argument('--device',choices=('cpu','gpu'),required=True)
    run.add_argument('--workers',type=int,default=1)
    run.add_argument('--points-only',action='store_true')
    run.add_argument('--profile',action='store_true')
    run.add_argument('--first-seed-only',action='store_true',help='pilot only, never five-seed evidence')
    compare=commands.add_parser('compare')
    compare.add_argument('--cpu',required=True)
    compare.add_argument('--gpu',required=True)
    compare.add_argument('--output',required=True)
    cost=commands.add_parser('cost')
    cost.add_argument('--study',required=True)
    cost.add_argument('--output',required=True)
    for key in ('cu-before','cu-after','allocation-seconds','setup-seconds'):
        cost.add_argument('--'+key,type=float,required=True)
    args=parser.parse_args(argv)
    if args.command=='run':
        report=run_study(load(args.config),args.output,device=args.device,workers=args.workers,
            statistics=not args.points_only,profile=args.profile,first_seed_only=args.first_seed_only)
        print(json.dumps(dict(status=report['status'],rows=len(report['rows']),skipped=report['skipped'])))
        return 1 if report['status']=='FAILED' else 0
    if args.command=='compare':
        cpu,gpu=load(args.cpu),load(args.gpu)
        write_json(args.output,dict(statistics=compare_studies(cpu,gpu),
            performance=performance_comparison(cpu,gpu)))
    else:
        write_json(args.output,cu_cost(load(args.study),cu_before=args.cu_before,cu_after=args.cu_after,
            allocation_seconds=args.allocation_seconds,setup_seconds=args.setup_seconds))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
