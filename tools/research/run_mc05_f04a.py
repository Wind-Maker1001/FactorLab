"""Run the frozen MC-05-F04A paired Grid Paper experiment. NO LIVE MONEY."""
import argparse
from pathlib import Path

from factorlab.experiments.mc_grid_tail_f04a import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('pilot', 'formal'))
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--calibration', type=Path)
    args = parser.parse_args()
    if (args.phase == 'formal') != (args.calibration is not None):
        parser.error('--calibration is required for formal and unavailable for pilot')
    run(args.output, snapshot=args.snapshot, design_path=Path('configs/mc05_f04a_grid_tail.json'),
        calibration_path=args.calibration)


if __name__ == '__main__':
    main()
