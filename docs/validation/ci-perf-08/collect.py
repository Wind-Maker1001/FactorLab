"""Collect a pinned Actions attempt without changing repository configuration."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import zipfile

REPO = 'Wind-Maker1001/FactorLab'


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path]))


def pages(path, key):
    result = []
    page = 1
    while True:
        data = api(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')
        rows = data[key] if key else data
        result.extend(rows)
        if len(rows) < 100:
            return result
        page += 1


def extract(archive, output):
    output.mkdir(exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        names = set()
        for entry in bundle.infolist():
            target = (output / entry.filename).resolve()
            if (not target.is_relative_to(output.resolve()) or target in names or
                    '\\' in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('unsafe or repeated artifact member: ' + entry.filename)
            names.add(target)
        bundle.extractall(output)


def collect(run_id, attempt, output):
    output.mkdir(parents=True, exist_ok=False)
    prefix = f'repos/{REPO}/actions/runs/{run_id}'
    run = api(f'{prefix}/attempts/{attempt}')
    jobs = pages(f'{prefix}/attempts/{attempt}/jobs', 'jobs')
    artifacts = pages(f'{prefix}/artifacts', 'artifacts')
    statuses = pages(f'repos/{REPO}/commits/{run["head_sha"]}/statuses', None)
    records = dict(run=run, jobs={'jobs': jobs}, artifacts={'artifacts': artifacts}, statuses=statuses,
                   collection={'observed_at': datetime.now(timezone.utc).isoformat(),
                               'repository': REPO, 'run_id': run_id, 'attempt': attempt,
                               'artifact_scope': 'run-wide; receipt attempt must be verified'})
    for name, value in records.items():
        (output / f'{name}.json').write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    archives = output / 'archives'
    archives.mkdir()
    for artifact in artifacts:
        name = artifact['name']
        if not (name.startswith('extended-3.') or name in {
                'pr-risk', 'fast-pr-receipts', 'verified-pr-current-coverage',
                'pr-lock-ubuntu-latest', 'pr-lock-windows-latest', 'pr-package'}):
            continue
        if Path(name).name != name or '/' in name or '\\' in name or name in {'.', '..'}:
            raise ValueError('unsafe artifact name')
        archive = archives / f'{artifact["id"]}.zip'
        with archive.open('xb') as stream:
            subprocess.run(['gh', 'api', f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip'],
                           stdout=stream, check=True)
        extract(archive, output / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=int, required=True)
    parser.add_argument('--attempt', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.run, args.attempt, args.output)


if __name__ == '__main__':
    main()
