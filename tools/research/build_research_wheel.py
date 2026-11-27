"""Build a commit-pinned research wheel with registered configurations and provenance."""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


def build_wheel(repository, commit, output):
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('A full Git commit is required')
    repository = Path(repository).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = subprocess.check_output(['git', '-C', str(repository), 'archive', '--format=tar', commit])
    with tempfile.TemporaryDirectory(prefix='factorlab-research-build-') as temporary:
        source = Path(temporary)/'source'
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(source, filter='data')
        resources = source/'src/factorlab/resources'
        (resources/'research-build.json').write_text(json.dumps({'commit': commit}), encoding='utf-8')
        for name in ('colab_job.py', 'colab_resources.py'):
            (resources/name).write_bytes((source/'tools/research'/name).read_bytes())
        for name in ('mc_colab_02_reliability', 'mc_colab_02_scale'):
            (resources/(name+'.json')).write_bytes((source/'configs/research'/(name+'.json')).read_bytes())
        subprocess.run([sys.executable, '-m', 'build', '--wheel', '--outdir', str(output), str(source)], check=True)
    return list(output.glob('*.whl'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for path in build_wheel(args.repository, args.commit, args.output):
        print(path)


if __name__ == '__main__':
    main()
