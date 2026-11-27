"""Build, install and run a V2 StrategyPackage against synthetic observations."""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
from time import perf_counter

from examples.alpaca_balanced.strategy import BalancedPortfolio
from examples.external_strategy.strategy import ReferenceTrend, frozen_return_spec
from factorlab.strategies.loader import LoadedStrategy
from factorlab.strategies.package import build_package, install_package
from factorlab.strategies.protocol import V2
from factorlab.strategies.reference_data import reference_context
from factorlab.trading.storage import dumps


EXAMPLES = Path(__file__).resolve().parents[1]
REFERENCE = 'SYNTHETIC:REFERENCE'
EFFECTIVE = {
    '1': '2026-01-05T00:00:00+00:00',
    '2': '2026-01-12T00:00:00+00:00',
}


def example_manifest(example, *, commit, account_id='offline', model_version='1',
                     reference_source=REFERENCE):
    """Create a normal StrategyPackage manifest from the entrypoint declaration."""
    config = dict(account_id=account_id)
    model = None
    if example == 'a':
        spec = BalancedPortfolio(**config).describe()
        entry = 'alpaca_balanced.strategy:BalancedPortfolio'
    elif example == 'b':
        config['reference_source'] = reference_source
        spec = ReferenceTrend(**config).describe()
        entry = 'external_strategy.strategy:ReferenceTrend'
    else:
        config['version'] = model_version
        spec = frozen_return_spec(**config)
        entry = 'external_strategy.strategy:FrozenReturn'
        effective = datetime.fromisoformat(EFFECTIVE[model_version])
        model = dict(file=f'external_strategy/linear-{model_version}.json', model_id='synthetic-return',
                     version=model_version, trained_through=(effective - timedelta(days=1)).isoformat(),
                     effective_at=effective.isoformat())
    spec_fields = asdict(spec)
    requirements = {key: spec_fields[key] for key in ('data', 'schedule', 'state', 'capital', 'execution')}
    requirements['capital']['requested'] = str(spec.capital['requested'])
    files = {entry.split(':')[0].replace('.', '/') + '.py': ''}
    manifest = dict(schema_version=2, interface=V2, strategy_id=spec.strategy_id,
                    strategy_version=spec.version, market=spec.market, symbols=spec.symbols,
                    account_id=account_id, execution_mode=spec.decision_kind, requirements=requirements,
                    code_commit=commit, entrypoint=entry, files=files, configuration=config,
                    dependencies=[], runtime_profile='lightweight',
                    evidence={'scope': 'SYNTHETIC_SOFTWARE_ONLY'}, migration='wait_flat')
    if model is not None:
        manifest['model'] = model
        files[model['file']] = ''
    return manifest


def event_for(spec, as_of):
    def bars(source, interval, count):
        rows = []
        for index in range(count):
            closed = as_of - timedelta(seconds=interval * (count - index))
            payload = dict(close=Decimal(100 + index), interval_seconds=interval, price_basis='raw')
            rows.append(dict(source=source, observed_at=closed, effective_at=closed, payload=payload))
        return reference_context({'closed_bar': rows})

    interval = spec.schedule.get('interval_seconds', 86400)
    binding = dict(market=spec.market, account_id=spec.account_id, strategy_id=spec.strategy_id,
                   instance_id='offline-example', allocation_id='offline-allocation')
    event = dict(protocol=V2, binding=binding, event_id='synthetic:' + as_of.isoformat(),
                 event_time=as_of, data_type='closed_bar', account={}, allocation={},
                 symbols={symbol: bars('SYNTHETIC:TRADING', interval, spec.data['history_limit'])
                          for symbol in spec.symbols})
    if spec.data.get('references'):
        event['references'] = {
            symbol: bars(REFERENCE, streams['closed_bar']['interval_seconds'],
                         streams['closed_bar']['history_limit'])
            for symbol, streams in spec.data['references'].items()
        }
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('example', choices=('a', 'b', 'c'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--model-version', choices=EFFECTIVE, default='1')
    parser.add_argument('--code-commit', help='source commit when building from a checkout without Git')
    parser.add_argument('--measure', action='store_true', help='Linux elapsed time and peak RSS by phase')
    options = parser.parse_args()
    commit = options.code_commit
    if commit is None:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=EXAMPLES, text=True).strip()
    manifest = example_manifest(options.example, commit=commit, model_version=options.model_version)
    archive = options.directory / f'{options.example}-{options.model_version}.zip'
    manifest = build_package(EXAMPLES, manifest, archive)
    release, _ = install_package(archive, options.directory / 'releases')
    measurements = {}

    def record(phase, started):
        if options.measure:
            import resource

            measurements[phase] = dict(elapsed_ms=round(1000 * (perf_counter() - started), 2),
                parent_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                child_peak_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)

    started = perf_counter()
    strategy = LoadedStrategy.from_strategy_package(release, trusted=True)
    record('load', started)
    as_of = datetime(2026, 1, 13, 15, 30, tzinfo=timezone.utc)
    if options.example == 'c' and options.model_version == '1':
        as_of -= timedelta(days=7)
    started = perf_counter()
    event = event_for(strategy.spec, as_of)
    record('warmup_projection', started)
    started = perf_counter()
    decision = strategy.decide(event, {})
    record('decision', started)
    print(dumps(dict(scope='SYNTHETIC_SOFTWARE_ONLY', event_time=as_of, package_id=manifest['package_id'],
        model=strategy.binding.get('model'), targets=decision.targets, state=decision.state,
        measurements=measurements)))


if __name__ == '__main__':
    main()
