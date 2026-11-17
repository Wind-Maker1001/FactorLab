"""Validate the original MC-05 evidence using independently frozen byte hashes."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat


MANIFEST = Path(__file__).with_name('reference') / 'mc05_frozen_manifest.json'
ORIGINAL_PATHS = frozenset({
    'configs/mc05_grid_tail.json',
    'docs/validation/mc-05/REPORT.md',
    *('docs/validation/mc-05/run/' + name for name in (
        'definition.json', 'deterministic.csv', 'generation.json', 'groups.json',
        'paths.csv', 'summary.json', 'tails.json')),
})


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate manifest key: {key}')
        result[key] = value
    return result


def verify_frozen(root, manifest=MANIFEST):
    root = Path(root)
    data = json.loads(Path(manifest).read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    assert type(data) is dict, 'manifest must be an object'
    assert set(data) == {'source_commit', 'source_kind', 'hash_algorithm',
                         'files', 'crlf_files', 'source_blobs'}, 'manifest fields'
    assert data['source_commit'] == '07821df1840a2c1b8432d7c1c0a6389179509ebe'
    assert data['source_kind'] == 'ORIGINAL_MC05_FROZEN_EVIDENCE'
    assert data['hash_algorithm'] == 'sha256'
    for field, length in (('files', 64), ('crlf_files', 64), ('source_blobs', 40)):
        entries = data[field]
        assert type(entries) is dict, field
        for name, digest in entries.items():
            path = PurePosixPath(name)
            assert not path.is_absolute() and '..' not in path.parts, name
            assert '\\' not in name and ':' not in name and str(path) == name, name
            assert isinstance(digest, str) and re.fullmatch(f'[0-9a-f]{{{length}}}', digest), name
        assert set(entries) == ORIGINAL_PATHS, f'{field}: original path set changed'

    def regular(path):
        assert stat.S_ISREG(path.lstat().st_mode), f'not a regular file: {path}'

    config = root / 'configs/mc05_grid_tail.json'
    assert stat.S_ISDIR((root / 'configs').lstat().st_mode), 'configs must be a directory'
    regular(config)
    actual = {'configs/mc05_grid_tail.json'}
    directory = root
    for part in ('docs', 'validation', 'mc-05'):
        directory = directory / part
        assert stat.S_ISDIR(directory.lstat().st_mode), f'not a directory: {directory}'

    def collect(directory):
        for path in directory.iterdir():
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                assert path.relative_to(root).as_posix() == 'docs/validation/mc-05/run', path
                collect(path)
            else:
                regular(path)
                actual.add(path.relative_to(root).as_posix())

    collect(directory)
    assert actual == ORIGINAL_PATHS, 'frozen evidence path set changed'
    for name in sorted(ORIGINAL_PATHS):
        digest = hashlib.sha256(root.joinpath(*PurePosixPath(name).parts).read_bytes()).hexdigest()
        assert digest in (data['files'][name], data['crlf_files'][name]), f'changed frozen bytes: {name}'
