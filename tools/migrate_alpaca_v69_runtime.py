"""Admit a new executor for the same active native V69 package under the Paper owner lock."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from factorlab.integrations.alpaca.cloud_runtime import owner_lock, private_root
from factorlab.integrations.alpaca.native_v69 import NATIVE_V69_WRAPPER
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.identity import content_identity
from factorlab.trading.storage import decode


_PROBE = """
from hashlib import file_digest
from importlib.metadata import version
from pathlib import Path
import platform
import sys

from factorlab.cross_market import calendar, contracts
from factorlab.integrations.alpaca.market_data import sessions
from factorlab.strategies import v69, v69_alpaca, v69_native, v69_runtime
from factorlab.strategies.loader import LoadedStrategy
from factorlab.trading.storage import dumps

strategy = LoadedStrategy.from_strategy_package(Path(sys.argv[1]), trusted=True)
sources = {}
for module in (v69, v69_alpaca, v69_native, v69_runtime, sessions, calendar, contracts):
    with Path(module.__file__).open('rb') as source:
        sources[module.__name__] = file_digest(source, 'sha256').hexdigest()
dependencies = {name: version(name) for name in ('numpy', 'pandas', 'python-dateutil', 'pytz', 'tzdata')}
print(dumps(dict(binding=strategy.binding, manifest=strategy.package_manifest, sources=sources,
                 dependencies=dependencies, python=platform.python_version())))
"""


def probe(python, package):
    result = subprocess.check_output([str(python), '-I', '-c', _PROBE, str(package)], text=True, timeout=60)
    return decode(json.loads(result))


def migrate(root, previous_python):
    root = private_root(root)
    with owner_lock(root), closing(DeploymentStore(root / 'deployments.sqlite3',
                                                   instance_id='V69_CAUSAL')) as store:
        package_id = store.snapshot()['active']
        manifest = store.manifest(package_id)
        if (manifest['evidence'].get('native_strategy') != 'V69_CAUSAL'
                or manifest['entrypoint'] != 'strategy:Strategy'
                or manifest['files'] != {'strategy.py': sha256(NATIVE_V69_WRAPPER).hexdigest()}
                or store.binding()['allocation_id'] != 'v69-original'):
            raise ValueError('active native V69 package and original allocation are required')
        for path in (root / 'signals').glob('*.native.json'):
            event = decode(json.loads(path.read_bytes()))
            if store.decision(package_id, event['event_id'], content_identity(event)) is None:
                raise ValueError(f'previous runtime must finish the frozen event in {path.name}')
        package = root / 'strategy-releases' / package_id
        previous = probe(previous_python, package)
        replacement = probe(sys.executable, package)
        if previous['manifest'] != manifest or replacement['manifest'] != manifest:
            raise ValueError('native V69 manifest differs from the active package')
        for key in ('sources', 'dependencies', 'python'):
            if previous[key] != replacement[key]:
                raise ValueError(f'native V69 {key} changed')
        created = store.admit_runtime_upgrade(package_id, previous['binding'], replacement['binding'])
        return dict(package_id=package_id, previous_runtime_sha256=previous['binding']['runtime_sha256'],
                    runtime_sha256=replacement['binding']['runtime_sha256'], admission_created=created)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--previous-python', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.state_root, args.previous_python), sort_keys=True))


if __name__ == '__main__':
    main()
