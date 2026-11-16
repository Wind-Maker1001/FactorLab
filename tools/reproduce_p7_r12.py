"""Reproduce compact R12 evidence in a new output directory, without networking."""
import argparse
import json
from pathlib import Path
import platform
import tempfile

from factorlab.dataset.identity import file_hash
from factorlab.experiments.grid_forward_evidence import evaluate, publish, read_m13


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tree', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=False)
    protocol = root / 'docs/validation/p7-r11/protocol.json'
    m13 = root / 'docs/validation/p7-m13/real/events.csv'
    summary = root / 'docs/validation/p7-m13/real/summary.json'
    synthetic = root / 'docs/validation/p7-r12/synthetic-shadow.json'
    adapter = root / 'src/factorlab/experiments/grid_forward_evidence.py'
    code = dict(commit=args.commit, tree=args.tree, adapter_sha256=file_hash(adapter))
    record = json.loads(protocol.read_text(encoding='utf-8'))
    real = read_m13(m13, start='2026-11-15T12:26:00Z', end='2026-11-15T12:30:00Z',
                    max_quote_gap_seconds=30)
    real['summary'] = json.loads(summary.read_text(encoding='utf-8'))
    artificial = json.loads(synthetic.read_text(encoding='utf-8'))
    for name, evidence in [('m13', real), ('synthetic', artificial)]:
        result = evaluate(record, evidence, evaluated_at='2026-11-15T12:31:00Z')
        with tempfile.TemporaryDirectory(prefix='factorlab-r12-') as store:
            records = publish(store, record, evidence, result, code=code,
                              environment=dict(python=platform.python_version(),
                                               evidence_origin='REAL_SHADOW' if name == 'm13'
                                               else 'SYNTHETIC_ENGINEERING_ONLY'),
                              command=['python', 'tools/reproduce_p7_r12.py',
                                       '--commit', args.commit, '--tree', args.tree,
                                       '--output', '<new-output-directory>'],
                              test_status='NOT_RUN')
        for suffix, value in [('result', result), *records.items()]:
            with (args.output / f'{name}-{suffix}.json').open('x', encoding='utf-8') as stream:
                json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write('\n')
    manifest = dict(code=code, inputs={str(path.relative_to(root)): file_hash(path)
                                      for path in (protocol, m13, summary, synthetic)})
    with (args.output / 'inputs.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print('Reproduced M13 rejection and synthetic engineering observation:', args.output)


if __name__ == '__main__':
    main()
