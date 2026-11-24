from argparse import Namespace
from pathlib import Path
import runpy
import subprocess
import io
import tarfile


def test_release_archive_preserves_linux_installer_line_endings(tmp_path):
    root = Path(__file__).parents[2]
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitattributes').write_bytes((root / '.gitattributes').read_bytes())
    (tmp_path / 'install.sh').write_bytes(b'#!/bin/sh\r\nset -eu\r\n')
    git = ['git', '-C', str(tmp_path), '-c', 'core.autocrlf=true']
    subprocess.run(git + ['add', '.'], check=True, capture_output=True)
    subprocess.run(git + ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                         'commit', '-qm', 'fixture'], check=True, capture_output=True)
    archive = subprocess.check_output(git + ['archive', 'HEAD'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        assert bundle.extractfile('install.sh').read() == b'#!/bin/sh\nset -eu\n'


def test_deploy_uses_committed_installer_not_working_tree(tmp_path, monkeypatch):
    deploy = runpy.run_path(str(Path(__file__).parents[2] / 'tools/deploy_alpaca_paper_runtime.py'))['deploy']
    revision = 'a' * 40
    commands = []
    monkeypatch.setattr(subprocess, 'check_output', lambda *_, **__: revision + '\n')
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: commands.append(command))
    deploy(Namespace(repo=str(tmp_path), commit=revision, key='key', user='azureuser',
                     host='example.invalid', credential_file=None))
    scp = next(command for command in commands if command[0] == 'scp')
    assert len(scp) == 6
    assert all('install_alpaca_paper_runtime.sh' not in part for part in scp)
    install = next(command[-1] for command in commands if command[0] == 'ssh' and 'sudo sh' in command[-1])
    remote = scp[-1].split(':', 1)[1].rstrip('/')
    assert remote.startswith(f'/tmp/factorlab-alpaca-{revision}-')
    assert f'tar -xf {remote}/release.tar' in install
    assert f'sudo sh {remote}/tools/install_alpaca_paper_runtime.sh' in install
    commands.clear()
    deploy(Namespace(repo=str(tmp_path), commit=revision, key='key', user='azureuser',
                     host='example.invalid', credential_file=None))
    second = next(command for command in commands if command[0] == 'scp')
    assert second[-1] != scp[-1]
