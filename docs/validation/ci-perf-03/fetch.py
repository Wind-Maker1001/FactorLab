"""Download the pinned CI evidence to fresh directories using authenticated gh."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

REPO = 'repos/Wind-Maker1001/FactorLab'
HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', choices=['43717685960', '43721499381'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for name, suffix in [('run', ''), ('jobs', '/jobs'), ('artifacts', '/artifacts')]:
        data = subprocess.check_output(['gh', 'api', f'{REPO}/actions/runs/{args.run}{suffix}'])
        (args.output / f'{name}.json').write_bytes(data)
    manifest = json.loads((HERE / f'provenance-{args.run}.json').read_bytes())
    for name, artifact in sorted(manifest['artifacts'].items()):
        if not (name.startswith('extended-3.') or name == 'verified-pr-current-coverage'):
            continue
        folder = args.output / name
        folder.mkdir(exist_ok=False)
        archive = folder / 'download.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh', 'api', f'{REPO}/actions/artifacts/{artifact["id"]}/zip'], stdout=stream, check=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                destination = (folder / member.filename).resolve()
                if not destination.is_relative_to(folder.resolve()):
                    raise ValueError('archive member escapes artifact directory')
            bundle.extractall(folder)
        for record in manifest['files']:
            if record['path'].startswith(name + '/'):
                path = args.output / record['path']
                if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                    raise ValueError('download differs from pinned evidence: ' + record['path'])
    print(args.output)


if __name__ == '__main__':
    main()
