"""Reproduce the two frozen linear artifacts from synthetic offline training rows."""
import argparse
import json
from pathlib import Path
from statistics import mean


TRAINING = {
    '1': [(-.02, -.01), (0, 0), (.02, .01)],
    '2': [(-.02, .01), (0, 0), (.02, -.01)],
}


def fit(rows):
    x, y = zip(*rows)
    x_mean, y_mean = mean(x), mean(y)
    coefficient = sum((a - x_mean) * (b - y_mean) for a, b in rows) / sum((a - x_mean) ** 2 for a in x)
    return dict(schema='FACTORLAB_LINEAR_MODEL_V1', features=['return_1'],
                coefficients=[coefficient], intercept=y_mean - coefficient * x_mean)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', choices=TRAINING)
    parser.add_argument('output', type=Path)
    options = parser.parse_args()
    with options.output.open('x', encoding='utf-8') as stream:
        json.dump(fit(TRAINING[options.version]), stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
