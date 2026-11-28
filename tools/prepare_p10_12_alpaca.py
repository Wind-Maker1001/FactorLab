"""Prepare native acceptance packages and shared-owner configuration without account IO.

Use the unchanged V69 native package already installed on the execution host.
The benchmark path must reference a causal ^SP500TR snapshot on that host.
Funding assumes the verified P10-12 baseline: no allocations, zero V69 inventory
and zero V69 economic cash postings. This is not a legacy-capital migration tool.
The proposed configuration is not deployable: shared symbol ceilings currently
collapse to the smallest sleeve limit. TODO: separate aggregate and instance
limits before activating the proposed three-instance configuration.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path

from factorlab.strategies.v179_v182_runtime import build_blend_strategy_package


def prepare(*, account_id, revision, artifact, destination, releases, benchmark,
            account_budget, fee_rate):
    if account_budget <= Decimal(100):
        raise ValueError('existing budget must cover V69 and both acceptance sleeves')
    destination.mkdir(parents=True, exist_ok=False)
    packages = {}
    registrations = [dict(binding=dict(market='ALPACA_US_EQUITY', account_id=account_id,
        strategy_id='V69_CAUSAL', instance_id='V69_CAUSAL', allocation_id='v69-original'),
        native_v69=True, funding=dict(capital=str(account_budget - Decimal(100)),
                                     initial_cash=str(account_budget - Decimal(100))))]
    for variant in ('v179', 'v182'):
        manifest = build_blend_strategy_package(variant=variant, account_id=account_id,
            capital='50', model_artifact=artifact, code_commit=revision,
            destination=destination / (variant + '.zip'),
            evidence=dict(purpose='P10-12_ENGINEERING_ACCEPTANCE', champion_promotion=False))
        packages[variant] = manifest['package_id']
        registrations.append(dict(binding=dict(market='ALPACA_US_EQUITY', account_id=account_id,
            strategy_id=manifest['strategy_id'], instance_id='P10-12-' + variant.upper(),
            allocation_id='p10-12-' + variant), funding=dict(capital='50', initial_cash='50')))
    config = dict(releases=str(releases), trusted_python=True,
        capital_policy=dict(currency='USD', budget=str(account_budget), fee_rate=str(fee_rate),
                            mode='SHARED_SAME_DIRECTION'),
        benchmark_source=dict(path=str(benchmark), series_id='^SP500TR'), instances=registrations)
    (destination / 'proposed-instances.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    (destination / 'packages.json').write_text(json.dumps(packages, indent=2) + '\n', encoding='utf-8')
    return packages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--remote-releases', required=True)
    parser.add_argument('--remote-benchmark', required=True)
    parser.add_argument('--existing-account-budget', type=Decimal, required=True)
    parser.add_argument('--fee-rate', type=Decimal, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(account_id=args.account_id, revision=args.revision,
        artifact=json.loads(args.artifact.read_text(encoding='utf-8')), destination=args.destination,
        releases=args.remote_releases, benchmark=args.remote_benchmark,
        account_budget=args.existing_account_budget, fee_rate=args.fee_rate)))


if __name__ == '__main__':
    main()
