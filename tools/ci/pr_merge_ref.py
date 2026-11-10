"""Construct the immutable event merge without consulting GitHub's merge ref."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


MESSAGE = 'FactorLab immutable event merge v1\n'


def reconstruct(repository, event, *, expected_subject=None, timeout=90):
    pr = event.get('pull_request')
    if not pr:
        return None
    if type(event['number']) is not int or event['number'] <= 0:
        raise ValueError('invalid event PR number')
    parents = [pr['base']['sha'], pr['head']['sha']]
    if any(not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha) for sha in parents):
        raise ValueError('event requires full commit SHAs')
    if expected_subject is not None and not re.fullmatch('[0-9a-f]{40}', expected_subject):
        raise ValueError('invalid pinned subject')
    repository = Path(repository).resolve()
    deadline = time.monotonic() + timeout
    # No candidate worktree, repository configuration, attributes file, or Git
    # environment is inherited by the merge engine. Attributes in Git trees
    # cannot register external drivers without configuration.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM='1', GIT_NO_REPLACE_OBJECTS='1',
               GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='FactorLab CI',
               GIT_AUTHOR_EMAIL='ci@factorlab.invalid', GIT_COMMITTER_NAME='FactorLab CI',
               GIT_COMMITTER_EMAIL='ci@factorlab.invalid',
               GIT_AUTHOR_DATE='946684800 +0000', GIT_COMMITTER_DATE='946684800 +0000',
               LC_ALL='C')

    def git(root, *args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('event merge construction timed out')
        return subprocess.check_output(
            ['git', '-C', str(root), '-c', f'core.hooksPath={os.devnull}',
             '-c', f'core.attributesFile={os.devnull}', '-c', 'commit.gpgSign=false',
             '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false',
             '-c', 'core.ignoreCase=false', '-c', 'core.fileMode=true',
             '-c', 'merge.renames=true', '-c', 'merge.renameLimit=7000',
             '-c', 'merge.renormalize=false', '-c', 'i18n.commitEncoding=UTF-8', *args],
            env=env, text=True, encoding='utf-8', timeout=remaining).strip()

    remote = git(repository, 'remote', 'get-url', 'origin')
    with tempfile.TemporaryDirectory(prefix='factorlab-event-merge-') as directory:
        objects = Path(directory)
        git(objects, 'init', '--bare', '--template=', '--object-format=sha1', '.')
        git(objects, 'fetch', '--no-tags', '--no-recurse-submodules', remote, *parents)
        for sha in parents:
            if git(objects, 'cat-file', '-t', sha) != 'commit':
                raise ValueError('event object is not a commit')
        # check_output rejects merge-tree's conflict exit status even when it
        # prints a tree. No conflict tree is ever committed or checked out.
        tree = git(objects, 'merge-tree', '--write-tree', '--no-messages', *parents)
        sha = git(objects, 'commit-tree', tree, '-p', parents[0], '-p', parents[1], '-m', MESSAGE)
        if git(objects, 'show', '-s', '--format=%P', sha).split() != parents:
            raise ValueError('constructed merge parents do not match event')
        if expected_subject is not None and sha != expected_subject:
            raise ValueError('reconstructed subject does not match pinned subject')
        git(objects, 'update-ref', 'refs/factorlab/event-merge', sha)
        git(repository, 'fetch', '--no-tags', '--no-recurse-submodules', str(objects),
            'refs/factorlab/event-merge')
        git(repository, '-c', 'core.autocrlf=' + ('true' if os.name == 'nt' else 'false'),
            'checkout', '--detach', sha)
    print(f'Event merge: {sha}; tree: {tree}; parents: {parents}', flush=True)
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--expected-subject')
    args = parser.parse_args()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    reconstruct(args.repository, event, expected_subject=args.expected_subject)


if __name__ == '__main__':
    main()
