"""Unified local research CLI; use python -I to select this checkout."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.research import workflow
from factorlab.errors import FactorLabError


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline research; NO LIVE MONEY / FORMAL_ADMISSION_BLOCKED')
    commands = parser.add_subparsers(dest='operation', required=True)
    run = commands.add_parser('run')
    for name in ('config', 'runtime', 'output', 'store', 'experiment-id', 'run-id'):
        run.add_argument('--' + name, required=True)
    for name in ('inspect', 'verify', 'replay'):
        command = commands.add_parser(name)
        command.add_argument('--store', required=True)
        command.add_argument('--run-id', required=True)
        if name in ('verify', 'replay'):
            command.add_argument('--runtime', required=name == 'replay')
        if name == 'replay':
            command.add_argument('--output', required=True)
            command.add_argument('--new-run-id', required=True)
    compare = commands.add_parser('compare')
    compare.add_argument('--store', required=True)
    compare.add_argument('--run-ids', nargs='+', required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'run':
            result = workflow.run(args.config, args.runtime, output=args.output, store=args.store,
                experiment_id=args.experiment_id, run_id=args.run_id)
        elif args.operation == 'inspect':
            result = workflow.inspect(args.store, args.run_id)
        elif args.operation == 'compare':
            result = workflow.compare(args.store, args.run_ids)
        elif args.operation == 'verify':
            result = workflow.verify(args.store, args.run_id, runtime_path=args.runtime)
        else:
            result = workflow.replay(args.store, args.run_id, runtime_path=args.runtime,
                output=args.output, new_run_id=args.new_run_id)
    except (ValueError, OSError, FactorLabError) as exc:
        print(json.dumps(dict(status='REJECTED', reason=str(exc))), file=sys.stderr)
        return 2
    if args.operation in ('run', 'inspect'):
        record = result['payload']['run']
        shown = dict(run_id=result['id'], experiment_id=result['experiment_id'], status=record['status'],
            qualification=record['qualification'], validation=record['validation'],
            code={k: v for k, v in record['code'].items() if k != 'source_sha256'}, environment=record['environment'],
            metrics=record['portfolio_metrics'], artifacts=record['artifacts'])
    else:
        shown = result
    print(json.dumps(shown, ensure_ascii=False, allow_nan=False))
    if args.operation == 'run' and result['payload']['run']['status'] != 'SUCCEEDED':
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
