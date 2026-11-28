"""Queue frozen acceptance packages under the existing Paper owner; never promote Champion."""
import argparse
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path

from factorlab.integrations.alpaca.cloud_runtime import owner_lock, private_root
from factorlab.strategies.deployment import DeploymentStore
from factorlab.strategies.package import install_package, model_refresh_candidate
from factorlab.strategies.v179_v182_runtime import ASSETS


def activate(root, configuration, prepared):
    """Register prepared instances and replayable model-refresh requests without funding or orders.

    The caller must qualify the runtime transition and independently verify the
    aggregate risk policy before selecting this configuration for the owner.
    """
    limits = configuration['capital_policy']['symbol_limits']
    if set(limits) != set(ASSETS):
        raise ValueError('complete explicit aggregate symbol limits are required')
    root = private_root(root)
    with owner_lock(root), closing(DeploymentStore(root / 'deployments.sqlite3')) as registry:
        native = configuration['instances'][0]
        with closing(registry.for_instance('V69_CAUSAL')) as view:
            if not native.get('native_v69') or native['binding'] != view.binding() or not view.snapshot()['active']:
                raise ValueError('original active native V69 binding is required')
        identities = json.loads((prepared / 'packages.json').read_text(encoding='utf-8'))
        requests = {}
        for registration in configuration['instances'][1:]:
            binding = registration['binding']
            variant = binding['allocation_id'].removeprefix('p10-12-')
            if variant not in {'v179', 'v182'} or Decimal(registration['funding']['capital']) != 50:
                raise ValueError('only prepared 50 USD V179/V182 acceptance instances are supported')
            _, manifest = install_package(prepared / (variant + '.zip'), Path(configuration['releases']))
            if manifest['package_id'] != identities[variant] or manifest['evidence']['champion_promotion'] is not False:
                raise ValueError('prepared acceptance package identity differs')
            registry.register_instance(binding)
            with closing(registry.for_instance(binding['instance_id'])) as view:
                candidate = model_refresh_candidate(binding, manifest, expected_active=None,
                    selection_id='p10-12-initial-' + variant + '-' + manifest['package_id'])
                requests[binding['instance_id']] = view.request(manifest, candidate)
        return dict(model_refresh_requests=requests, champion_promoted=False,
                    capital_applied=False, official_orders_submitted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--configuration', required=True, type=Path)
    parser.add_argument('--prepared', required=True, type=Path)
    args = parser.parse_args()
    configuration = json.loads(args.configuration.read_text(encoding='utf-8'))
    print(json.dumps(activate(args.state_root, configuration, args.prepared), sort_keys=True))


if __name__ == '__main__':
    main()
