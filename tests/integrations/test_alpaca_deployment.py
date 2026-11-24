from argparse import Namespace
from pathlib import Path
import runpy
import subprocess


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
    assert f'tar -xf /tmp/factorlab-alpaca-{revision}/release.tar' in install
    assert f'sudo sh /tmp/factorlab-alpaca-{revision}/tools/install_alpaca_paper_runtime.sh' in install
