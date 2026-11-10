"""Real nested Windows Git checkout with long-path support disabled."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from factorlab.integrations.binance import Reader


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = Path('docs/validation/p6-b01')


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Git path-length regression')
def test_nested_checkout_and_real_archive_replay(tmp_path):
    repo = tmp_path / ('candidate-' + 'x' * (140 - len(str(tmp_path)) - 11))
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ['git', '-c', 'core.longpaths=false', '-c', 'core.autocrlf=false',
             '-C', str(repo), *args], text=True,
        ).strip()

    git('init')
    git('remote', 'add', 'origin', ROOT.as_uri())
    subject = subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True,
    ).strip()
    git('fetch', '--no-tags', '--depth=1', 'origin', subject)
    git('sparse-checkout', 'set', '--no-cone', '/docs/validation/p6-b01/')
    git('checkout', '--detach', subject)

    files = list((repo / ARCHIVE).rglob('*'))
    assert max(len(str(path)) for path in files) < 260
    old_receipt = (repo / ARCHIVE / 'nvdausdt/runs' / ('a' * 32)
                   / 'requests' / ('b' * 64 + '-' + 'c' * 32 + '.json'))
    assert len(str(old_receipt)) >= 260

    reader = Reader(repo / ARCHIVE / 'nvda')
    assert reader.verify_raw() == 6
    assert {kind: len(reader.read(kind).data) for kind in ('bars', 'funding', 'mark')} == {
        'bars': 10, 'funding': 30, 'mark': 1,
    }
    receipts = [json.loads(path.read_text())
                for path in (reader.root / 'requests').glob('*.json')
                if len(path.stem) != 64]
    assert sorted(receipts, key=lambda row: row['url']) == sorted(
        reader.manifest['raw_receipts'], key=lambda row: row['url'],
    )
