import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.research.mc05_frozen import MANIFEST, ORIGINAL_PATHS, verify_frozen


@pytest.fixture
def frozen_copy(tmp_path):
    root = tmp_path / 'without-git'
    source = Path(__file__).resolve().parents[2]
    for name in ORIGINAL_PATHS:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    manifest = tmp_path / 'manifest.json'
    shutil.copyfile(MANIFEST, manifest)
    return root, manifest


@pytest.mark.parametrize('newline', [b'\n', b'\r\n'])
def test_frozen_bytes_without_git_on_either_platform(frozen_copy, monkeypatch, newline):
    root, manifest = frozen_copy
    for name in ORIGINAL_PATHS:
        path = root / name
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', newline))
    def no_git(*args, **kwargs):
        pytest.fail('frozen validation must not execute Git')
    monkeypatch.setattr(subprocess, 'Popen', no_git)
    assert not (root / '.git').exists()
    verify_frozen(root, manifest)


@pytest.mark.parametrize('change', ['byte', 'delete', 'extra', 'hash', 'missing-entry',
                                    'algorithm', 'escape', 'backslash', 'directory',
                                    'duplicate', 'invalid-hash', 'list'])
def test_frozen_manifest_rejects_tampering(frozen_copy, change):
    root, manifest = frozen_copy
    name = 'docs/validation/mc-05/run/summary.json'
    path = root / name
    data = json.loads(manifest.read_text())
    if change == 'byte':
        value = path.read_bytes()
        path.write_bytes(bytes([value[0] ^ 1]) + value[1:])
    elif change == 'delete':
        path.unlink()
    elif change == 'extra':
        (path.parent / 'unexpected.json').write_bytes(b'{}')
    elif change == 'hash':
        data['files'][name] = data['crlf_files'][name] = '0' * 64
    elif change == 'missing-entry':
        for field in ('files', 'crlf_files', 'source_blobs'):
            del data[field][name]
    elif change == 'algorithm':
        data['hash_algorithm'] = 'md5'
    elif change in ('escape', 'backslash'):
        key = '../outside.json' if change == 'escape' else name.replace('/', '\\')
        data['files'][key] = data['files'].pop(name)
    elif change == 'directory':
        path.unlink()
        path.mkdir()
    elif change == 'invalid-hash':
        data['files'][name] = 42
    elif change == 'list':
        data = []
    if change == 'duplicate':
        manifest.write_text('{"files": {}, "files": {}}', encoding='utf-8')
    else:
        manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises((AssertionError, ValueError, FileNotFoundError)):
        verify_frozen(root, manifest)
