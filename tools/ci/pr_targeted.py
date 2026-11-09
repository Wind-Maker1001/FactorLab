"""Run changed standalone research tests in Fast CI, without market data privileges."""
import argparse
import ast
import base64
import json
from pathlib import Path
import subprocess
import sys

from tools.ci.pr_risk import targeted_path

ROOT = Path(__file__).resolve().parents[2]


def decode_paths(encoded):
    """Only accept a sorted, unique list from the trusted risk-selector output."""
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 100_000:
        raise ValueError('targeted list too large')
    paths = json.loads(raw)
    if (not isinstance(paths, list) or any(not isinstance(p, str) for p in paths)
            or paths != sorted(set(paths)) or
            any(not targeted_path(p) or Path(p).is_absolute() for p in paths)):
        raise ValueError('unsafe or duplicate targeted paths')
    for path in paths:
        source = ROOT / path
        if source.is_symlink() or not source.is_file():
            raise ValueError('missing or nonregular targeted source: ' + path)
    return paths


def run(paths, output):
    output.mkdir(parents=True, exist_ok=False)
    diagnostic_scripts = [p for p in paths if p.startswith('tools/data/')]
    tests = [p for p in paths if p.startswith('tests/evidence/')]
    # Never execute diagnostic scripts against private local data on a public CI runner.
    for path in diagnostic_scripts:
        source = (ROOT / path).read_text(encoding='utf-8-sig')
        ast.parse(source, filename=path)
        print('Diagnostic syntax PASS:', path, flush=True)
    command = ([sys.executable, '-m', 'pytest', '-q', *tests,
                '--junitxml=' + str(output / 'tests.xml')] if tests else None)
    code = 0
    with (output / 'tests.log').open('wb') as log:
        if command:
            print('Run changed evidence tests:', tests, flush=True)
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT) as process:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                code = process.wait()
        else:
            log.write(b'No changed evidence tests; diagnostic syntax checked.\n')
    (output / 'receipt.json').write_text(json.dumps(dict(
        schema='FACTORLAB_TARGETED_RESEARCH_CI_V1',
        status='PASS' if code == 0 else 'FAIL', exit_code=code,
        tests=tests, syntax_checked=diagnostic_scripts,
        financial_authorization='BLOCKED', no_live_money=True),
        indent=2) + '\n', encoding='utf-8')
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--encoded', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    return run(decode_paths(args.encoded), args.output.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
