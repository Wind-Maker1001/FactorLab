"""Load the base-side current selector without shadowing candidate tool packages."""
import argparse
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import sys

import pytest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trusted-shard-plan', type=Path, required=True)
    args, pytest_args = parser.parse_known_args()
    raw = args.trusted_shard_plan.read_bytes()
    if hashlib.sha256(raw).hexdigest() != os.environ['RISK_SHA256']:
        raise ValueError('current execution risk plan issuer digest mismatch')
    source = Path(__file__).with_name('current_validation.py')
    spec = importlib.util.spec_from_file_location(
        '_factorlab_trusted_current', source, submodule_search_locations=[str(source.parent)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    sharding = importlib.import_module(spec.name + '.sharding')
    plugin.TRUSTED_SPEC = sharding.read_json(raw)['sharding']
    plugin.validation_layers.ROOT = Path.cwd()
    sys.path.insert(0, str(Path.cwd()))
    return pytest.main(pytest_args, plugins=[plugin])


if __name__ == '__main__':
    raise SystemExit(main())
