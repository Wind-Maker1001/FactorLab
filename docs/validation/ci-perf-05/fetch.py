"""Fetch pinned CI attempt-1 evidence using authenticated gh; never execute it."""
import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import zipfile

REPO = 'repos/Wind-Maker1001/FactorLab'
RUNS = ('43735547182', '43735048156')
ATTEMPT = 1
ARTIFACT_NAMES = (
    'pr-risk', 'verified-pr-current-coverage', 'fast-pr-receipts',
    *(f'extended-{version}-shard-{shard}'
      for version in ('3.13', '3.14') for shard in range(3)),
    'pr-lock-windows-latest', 'pr-lock-ubuntu-latest', 'pr-package',
)


def api(endpoint, *, paginate=False):
    command = ['gh', 'api', endpoint]
    if paginate:
        command.extend(['--paginate', '--slurp'])
    return json.loads(subprocess.check_output(command))


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def extract(archive, destination):
    destination.mkdir(exist_ok=False)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            target = destination.joinpath(*path.parts)
            if (path.is_absolute() or '..' in path.parts
                    or '\\' in member.filename or ':' in member.filename
                    or not path.parts
                    or not target.resolve().is_relative_to(root)
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ValueError(f'unsafe archive member: {member.filename!r}')
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)


def fetch(run_id, output):
    output.mkdir(parents=True, exist_ok=False)
    endpoint = f'{REPO}/actions/runs/{run_id}'
    run = api(endpoint)
    write_json(output / 'run.json', run)
    write_json(output / 'commit-statuses.json', api(f'{REPO}/commits/{run["head_sha"]}/statuses?per_page=100'))
    if run['id'] != int(run_id) or run['run_attempt'] != ATTEMPT:
        raise ValueError('pinned run must still have exactly attempt 1')
    jobs_pages = api(f'{endpoint}/attempts/{ATTEMPT}/jobs?per_page=100', paginate=True)
    artifact_pages = api(f'{endpoint}/artifacts?per_page=100', paginate=True)
    write_json(output / 'jobs.json', {
        'total_count': jobs_pages[0]['total_count'],
        'jobs': [job for page in jobs_pages for job in page['jobs']],
    })
    artifacts = [artifact for page in artifact_pages for artifact in page['artifacts']]
    write_json(output / 'artifacts.json', {
        'total_count': artifact_pages[0]['total_count'], 'artifacts': artifacts,
    })
    selected = {}
    for name in ARTIFACT_NAMES:
        matches = [artifact for artifact in artifacts if artifact['name'] == name]
        if len(matches) != 1:
            raise ValueError(f'expected exactly one artifact named {name!r}')
        artifact = matches[0]
        if artifact['expired'] or not isinstance(artifact['id'], int) or artifact['id'] <= 0:
            raise ValueError(f'artifact is expired or has an invalid ID: {name}')
        selected[name] = artifact
    # Pin the IDs resolved from this run's API listing before any archive request.
    write_json(output / 'provenance.json', {
        'repository': REPO.removeprefix('repos/'),
        'run_id': int(run_id), 'run_attempt': ATTEMPT,
        'artifacts': selected,
    })
    for name, artifact in selected.items():
        folder = output / 'archives' / name
        folder.mkdir(parents=True, exist_ok=False)
        archive = folder / 'download.zip'
        with archive.open('xb') as stream:
            subprocess.run([
                'gh', 'api', f'{REPO}/actions/artifacts/{artifact["id"]}/zip',
            ], stdout=stream, check=True)
        extract(archive, output / name)
    print(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', choices=RUNS, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='new, nonexistent directory for this run')
    args = parser.parse_args()
    fetch(args.run, args.output.resolve())


if __name__ == '__main__':
    main()
