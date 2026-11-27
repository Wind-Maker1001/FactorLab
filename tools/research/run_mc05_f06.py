"""Run the F06 static-grid study with local raw arrays. NO LIVE MONEY."""
import argparse
from pathlib import Path

from factorlab.experiments.mc05_f06 import load_definition, run_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, default=Path('configs/research/mc05_f06.json'))
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--steps', type=int, choices=(168, 720, 2160), default=168)
    parser.add_argument('--sizes', type=int, nargs='+')
    parser.add_argument('--models', nargs='+')
    parser.add_argument('--seeds', type=int, nargs='+')
    parser.add_argument('--backend', choices=('cpu', 'cuda'), default='cuda')
    parser.add_argument('--no-audits', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(args.definition, root)
    run_study(args.output, definition=definition, snapshot=args.snapshot, root=root,
        steps=args.steps, sizes=args.sizes, models=args.models, seeds=args.seeds,
        backend=args.backend, audits=not args.no_audits)


if __name__ == '__main__':
    main()
