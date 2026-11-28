"""Admit a new executor for the same active native V69 package under the Paper owner lock."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sys
import subprocess
from copy import deepcopy

from factorlab.integrations.alpaca.cloud_runtime import owner_lock, private_root
from factorlab.integrations.alpaca.native_v69 import NATIVE_V69_WRAPPER
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.identity import content_identity
from factorlab.strategies.runtime_migration import probe, require_compatible
from factorlab.trading.storage import decode


# Exact economic-policy transition; these are not lifecycle exemptions.
_POLICY_SOURCES = {
    'factorlab.trading.capital': (
        '4fdda3534fad2339c02208011ecba937cb4576b7078c22aed25ff90b85803c1d',
        'abe021468f812a5b9a4e6d643ce7a3c207c46f5bd89de7d0e19cbf7e39247fa7'),
    'factorlab.integrations.alpaca.strategy_instances': (
        '67a4053e847a583c37f97ed1917ee1cf3b8e3bbef10cda6c6b6cbbab3c6ab3e7',
        '38a46a599e2de6de768a1adfe7c434bd5097a6c5a61449aecfb7ef7a98abd367'),
}


def policy_probe(python, package):
    from factorlab.strategies.runtime_migration import _PROBE, _AUDITED
    # The identical probe retains its compute traversal separately from host roots.
    script = _PROBE + '\nprint(json.dumps(sorted(visited)))\n'
    output = subprocess.check_output(
        [str(python), '-I', '-c', script, str(package), json.dumps(_AUDITED)],
        text=True, timeout=300)
    result, closure = output.splitlines()
    return decode(json.loads(result)), set(json.loads(closure))


def require_policy_transition(previous, replacement, manifest, previous_closure, replacement_closure):
    if set(_POLICY_SOURCES) & (previous_closure | replacement_closure):
        raise ValueError('host policy transition changes strategy compute closure')
    normalized = deepcopy(replacement)
    for name, (before, after) in _POLICY_SOURCES.items():
        if previous['sources'].get(name) != before or replacement['sources'].get(name) != after:
            raise ValueError('unsupported host policy source pair: ' + name)
        normalized['sources'][name] = before
    require_compatible(previous, normalized, manifest)
    return dict(kind='HOST_POLICY_CHANGE', source_pairs={
        name: dict(previous=before, replacement=after)
        for name, (before, after) in _POLICY_SOURCES.items()})


def migrate(root, previous_python, *, host_policy_transition=False):
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
        qualification = dict(kind='COMPUTE_COMPATIBLE')
        if host_policy_transition:
            previous, previous_closure = policy_probe(previous_python, package)
            replacement, replacement_closure = policy_probe(sys.executable, package)
            qualification = require_policy_transition(
                previous, replacement, manifest, previous_closure, replacement_closure)
        else:
            previous = probe(previous_python, package)
            replacement = probe(sys.executable, package)
            require_compatible(previous, replacement, manifest)
        created = store.admit_runtime_upgrade(package_id, previous['binding'], replacement['binding'])
        return dict(package_id=package_id, previous_runtime_sha256=previous['binding']['runtime_sha256'],
                    runtime_sha256=replacement['binding']['runtime_sha256'], admission_created=created,
                    qualification=qualification)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--previous-python', required=True, type=Path)
    parser.add_argument('--p10-12-host-policy-transition', action='store_true',
                        help='qualify the exact reviewed explicit aggregate-limit source transition')
    args = parser.parse_args()
    print(json.dumps(migrate(args.state_root, args.previous_python,
                           host_policy_transition=args.p10_12_host_policy_transition), sort_keys=True))


if __name__ == '__main__':
    main()
