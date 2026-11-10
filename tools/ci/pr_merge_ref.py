"""Wait for GitHub's published merge commit to match the immutable PR event."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def refresh(repository, event, *, timeout=45, interval=5):
    pr = event.get('pull_request')
    if not pr:
        return None
    number = event['number']
    if type(number) is not int or number <= 0:
        raise ValueError('invalid event PR number')
    expected = [pr['base']['sha'], pr['head']['sha']]
    deadline = time.monotonic() + timeout

    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('timed out waiting for event-matching GitHub merge ref')
        return subprocess.check_output(
            ['git', '-C', str(repository), *args], text=True, timeout=remaining).strip()

    while True:
        git('fetch', '--no-tags', '--no-recurse-submodules', 'origin',
            f'+refs/pull/{number}/merge:refs/factorlab/event-merge')
        sha = git('rev-parse', '--verify', 'refs/factorlab/event-merge^{commit}')
        parents = git('show', '-s', '--format=%P', sha).split()
        if len(parents) != 2:
            raise ValueError(f'GitHub merge ref {sha} does not have two parents: {parents}')
        if parents == expected:
            git('-c', 'core.hooksPath=/dev/null', 'checkout', '--detach', sha)
            print(f'Event-matching GitHub merge: {sha}; parents: {parents}', flush=True)
            return sha
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'GitHub merge ref {sha} parents {parents} do not match event {expected}')
        print(f'Waiting for GitHub merge ref: {sha}; parents: {parents}; expected: {expected}', flush=True)
        time.sleep(min(interval, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    args = parser.parse_args()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    refresh(args.repository, event)


if __name__ == '__main__':
    main()
