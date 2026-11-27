"""Exercise interrupted and invalid offsite pulls without using a real SSH identity."""

from contextlib import closing
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess

import pytest

from factorlab.integrations import offsite_backup
from factorlab.integrations.binance_futures_trading import demo_cloud_storage as storage
from factorlab.integrations.binance_futures_trading.protocol import SafetyBlocked


FIRST = 'backup-20261126T030000Z'
SECOND = 'backup-20261126T030000Z-99'
THIRD = 'backup-20261126T030000Z-100'


def _published(root, name):
    snapshot = root / name
    snapshot.mkdir(mode=0o700)
    db = snapshot / 'runtime.sqlite3'
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('CREATE TABLE control (state TEXT)')
        connection.execute("INSERT INTO control VALUES ('STOPPED')")
        connection.commit()
    db.chmod(0o600)
    storage.atomic_json(snapshot / 'backup-manifest.json',
                        {'version': 1, 'databases': ['runtime.sqlite3'], 'binding': False})
    files = {path.name: storage.file_sha256(path) for path in snapshot.iterdir()}
    storage.atomic_json(snapshot / 'recovery-manifest.json', {
        'version': 1, 'market': 'alpaca', 'captured_at': '2026-11-26T03:00:00+00:00',
        'revision': '1' * 40, 'final_target': None, 'files': files,
    })
    return snapshot


def _stats(size=0, received=500):
    return f'Total file size: {size:,} bytes\nTotal bytes sent: 50\nTotal bytes received: {received:,}\n'


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / 'export'
    root.mkdir(mode=0o700)
    return root


@pytest.fixture
def receiver(tmp_path):
    return dict(root=tmp_path / 'received', host='dr-alpaca', market='alpaca', keep=1,
                max_snapshot_bytes=64 * 1024, ssh_config='/etc/factorlab-dr/ssh_config')


def test_latest_uses_numeric_suffix_and_reports_transfer_bytes(remote, receiver, monkeypatch):
    _published(remote, SECOND)
    newest = _published(remote, THIRD)

    def rsync(shell, *args):
        assert 'StrictHostKeyChecking=yes' in shell
        assert shell.endswith('-F /etc/factorlab-dr/ssh_config')
        if '--list-only' in args:
            assert args[-1] == 'dr-alpaca:./'
            listing = _stats()
            for name in (THIRD, SECOND):
                listing += f'drwx------ 4,096 2026/11/26 03:00:00 {name}\n'
            return listing
        assert args[-2] == f'dr-alpaca:{THIRD}/'
        if '--dry-run' in args:
            return _stats(9_000)
        shutil.copytree(newest, Path(args[-1]), dirs_exist_ok=True)
        return _stats(9_000, received=1_200)

    monkeypatch.setattr(offsite_backup, '_rsync', rsync)
    result = offsite_backup.pull_snapshot(source='.', latest=True, **receiver)

    assert result['snapshot'] == THIRD
    assert result['snapshot_bytes'] == 9_000
    assert result['rsync_sent_bytes'] == 150
    assert result['rsync_received_bytes'] == 2_200


def test_interruption_resumes_without_overwriting_the_previous_snapshot(remote, receiver, monkeypatch):
    _published(remote, FIRST)
    next_snapshot = _published(remote, SECOND)
    root = receiver['root']
    first = root / FIRST
    second = root / SECOND
    corrupt = False

    def rsync(shell, *args):
        if '--dry-run' in args:
            return _stats(9_000)
        source = remote / PurePosixPath(args[-2].rsplit(':', 1)[1]).name
        staging = Path(args[-1])
        if source == next_snapshot:
            assert f'--copy-dest={first}' in args
            assert '--partial' in args
            if not (staging / 'runtime.sqlite3').exists():
                prefix = (source / 'runtime.sqlite3').read_bytes()[:128]
                (staging / 'runtime.sqlite3').write_bytes(prefix)
                raise subprocess.CalledProcessError(12, 'rsync')
        shutil.copytree(source, staging, dirs_exist_ok=True)
        if corrupt:
            (staging / 'runtime.sqlite3').write_bytes(b'corrupt')
        return _stats(9_000)

    monkeypatch.setattr(offsite_backup, '_rsync', rsync)
    offsite_backup.pull_snapshot(source=FIRST, **receiver)
    with pytest.raises(subprocess.CalledProcessError):
        offsite_backup.pull_snapshot(source=SECOND, **receiver)
    assert first.is_dir()
    assert not second.exists()

    corrupt = True
    with pytest.raises(SafetyBlocked, match='inventory or digest'):
        offsite_backup.pull_snapshot(source=SECOND, **receiver)
    assert first.is_dir()
    assert not second.exists()

    corrupt = False
    result = offsite_backup.pull_snapshot(source=SECOND, **receiver)
    assert result['status'] == 'PUBLISHED'
    assert second.is_dir()
    assert not first.exists()
    with pytest.raises(SafetyBlocked, match='older than the latest'):
        offsite_backup.pull_snapshot(source=FIRST, **receiver)
    assert second.is_dir()


def test_oversize_source_stops_before_payload_copy(receiver, monkeypatch):
    def rsync(shell, *args):
        assert '--dry-run' in args
        return _stats(receiver['max_snapshot_bytes'] + 1)

    monkeypatch.setattr(offsite_backup, '_rsync', rsync)
    with pytest.raises(SafetyBlocked, match='byte limit'):
        offsite_backup.pull_snapshot(source=FIRST, **receiver)
    assert not (receiver['root'] / FIRST).exists()
