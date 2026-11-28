"""Prepare bounded decision-only Demo packages for independently funded acceptance."""
import argparse
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path

from factorlab.strategies.package import build_package
from factorlab.strategies.protocol import StrategySpecV2, V2
from factorlab.trading.storage import dumps


# TODO: Bind bounded acceptance packages through a qualified non-Champion admission path.
# MODEL_REFRESH requires a real frozen model artifact; these packages have none.
def prepare(directory, *, account_id, symbols, revision):
    directory.mkdir(parents=True)
    allocations, packages = [], []
    for label, assets, exit_after in (('one', symbols[:1], 3), ('two', symbols[1:], 5)):
        instance = 'p10-12-binance-' + label
        declaration = StrategySpecV2(strategy_id='p10-12-bounded-'+label, version='1',
            market='BINANCE_USDM', account_id=account_id, symbols=assets, decision_kind='target_weights',
            data=dict(data_types=['closed_bar'], history_limit=1, historical_observations=False),
            schedule=dict(kind='interval', interval_seconds=60),
            state=dict(version='1', migration='preserve', from_versions=[]),
            capital=dict(currency='USDT', requested=Decimal(10)),
            execution=dict(order_types=['LIMIT'], time_in_force=['GTC'], shorting=False,
                fractional_quantities=True, reduce_only=True))
        source = directory/label
        source.mkdir()
        (source/'strategy.py').write_text(
            'from decimal import Decimal\n'
            'from factorlab.strategies.protocol import StrategySpecV2, DecisionV2\n'
            'class Strategy:\n'
            '    def describe(self):\n        return StrategySpecV2(**'+repr(asdict(declaration))+')\n'
            '    def decide(self, event, state):\n'
            '        count = state.get("bars", 0) + 1\n'
            '        weight = Decimal("0.6") if count <= '+str(exit_after)+' else Decimal(0)\n'
            '        return DecisionV2("target_weights", targets={symbol: weight for symbol in '+repr(assets)+'}, state={"bars": count})\n'
            '    def migrate(self, state, from_version):\n        return state\n', encoding='utf-8')
        data = asdict(declaration)
        requirements = {key:data[key] for key in ('data','schedule','state','capital','execution')}
        requirements['capital'] = dict(requirements['capital'], requested='10')
        manifest = dict(schema_version=2, interface=V2, strategy_id=declaration.strategy_id,
            strategy_version='1', market=declaration.market, account_id=account_id, symbols=assets,
            execution_mode='target_weights', requirements=requirements, code_commit=revision,
            entrypoint='strategy:Strategy', files={'strategy.py':''}, configuration={}, dependencies=[],
            evidence={'champion_promotion':False,'bounded_demo_acceptance':True}, migration='wait_flat')
        manifest = build_package(source, manifest, directory/(label+'.zip'))
        allocation = dict(instance_id=instance, allocation_id=instance+'-new20',
            strategy_id=declaration.strategy_id, capital=Decimal(10))
        allocations.append(allocation)
        packages.append(dict(archive=label+'.zip', package_id=manifest['package_id'],
            binding=dict(market='BINANCE_USDM',account_id=account_id,instance_id=instance,
                allocation_id=allocation['allocation_id'],strategy_id=declaration.strategy_id)))
    (directory/'capital.json').write_text(dumps(dict(allocations=allocations, symbols=symbols,
        fee_rate=Decimal('.001'))), encoding='utf-8')
    (directory/'packages.json').write_text(json.dumps(packages,indent=2),encoding='utf-8')
    return dict(instances=len(packages),capital='20',account_writes=0)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--account-id',required=True)
    parser.add_argument('--symbols',nargs=2,required=True)
    parser.add_argument('--revision',required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.directory,account_id=args.account_id,symbols=args.symbols,revision=args.revision)))
