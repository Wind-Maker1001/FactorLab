"""Bounded, explicitly routed D06 inputs; inventory pins never grant admission."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from audit_p4_01_pipeline_inputs import _D07ResultAdapter, d07_reference
from d06_readiness_adapter import load_research
from factorlab.application.research_pipeline import HubQuery
from factorlab.data.evidence import sha256
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.diagnostics.pipeline_preflight import (
    FIELDS, GROUPS, NUMERIC, InputBinding, content_identity, pipeline_preflight,
)

ROOT = Path(__file__).resolve().parents[2]
KINDS = dict(bars='daily_bars', benchmark='index_bars', membership='membership',
             status='security_status', listings='listings', calendar='calendar')
SOURCE_IDS = {'bigquant': 'BigQuant', 'joinquant': 'JoinQuant_OBSERVED_MEMBERSHIP'}
IMPLEMENTATION = (
    'tools/data/audit_p4_03_controlled_inputs.py',
    'tools/data/audit_p4_01_pipeline_inputs.py', 'tools/data/d06_readiness_adapter.py',
    'tools/data/audit_p3_d06.py', 'src/factorlab/diagnostics/pipeline_preflight.py',
    'src/factorlab/data/hub/hub.py', 'src/factorlab/data/hub/contracts.py',
    'src/factorlab/data/units.py',
)


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as target:
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        target.write('\n')


class ExactRoutes:
    """Expose only the selected source/kind/security/window, without fallback."""

    def __init__(self, routes):
        self.routes = routes
        self.capabilities = frozenset(key[0] for key in routes)

    def read(self, kind, *, symbol, start, end, knowledge_as_of, security_id=None):
        if knowledge_as_of is not None:
            raise UnsupportedQueryError('historical knowledge UNKNOWN; retrospective diagnostics only')
        key = (kind, symbol, start, end)
        if key not in self.routes:
            raise UnsupportedQueryError('query outside explicitly registered source/security/window')
        return self.routes[key]


def scoped(result, group, symbol, window):
    # Reuse P4-01's calendar envelope and scoped research-table identity rules.
    return _D07ResultAdapter({group: result}).read(group, symbol=symbol,
        start=window['start'], end=window['end'], knowledge_as_of=None)


def source_identities(inputs, bigquant_root, membership_zip):
    """Bind schema and receipt bytes as well as D06's verified payload hashes."""
    identities = {}
    for result in inputs.values():
        refs = result.metadata.provenance['input_members']
        if not isinstance(refs, list):
            continue
        for ref in refs:
            key = (ref['archive'], ref['member'])
            if key in identities:
                continue
            with zipfile.ZipFile(Path(bigquant_root) / key[0]) as archive:
                receipt = archive.read(key[1] + '.receipt.json')
                schema_name = key[1].rsplit('/', 1)[0] + '/schema.json'
                schema = archive.read(schema_name)
            identities[key] = dict(archive=key[0], member=key[1],
                content_sha256=ref['actual_sha256'], receipt_sha256=sha256(receipt),
                schema_sha256=sha256(schema), exported_at_utc=ref['exported_at_utc'],
                physical_rows=ref['physical_rows'], uncompressed_bytes=ref['uncompressed_bytes'])
    with zipfile.ZipFile(membership_zip) as archive:
        manifest = archive.read('manifest.json')
        payload = archive.read('members_daily.csv')
    declared = json.loads(manifest.decode('utf-8-sig'))
    if sha256(payload) != declared['output_sha256']['members_daily.csv']:
        raise ValueError('membership payload differs from source manifest')
    identities[('membership', 'members_daily.csv')] = dict(
        archive='csi500_full_history.zip', member='members_daily.csv',
        content_sha256=sha256(payload), manifest_sha256=sha256(manifest),
        acquired_at_claim=declared['run_completed_utc'],
        historical_available_at=declared['historical_available_at'],
        requested_start=declared['requested_start'], requested_end=declared['requested_end'])
    return [identities[key] for key in sorted(identities)]


def load(definition, bigquant_root, membership_zip):
    _, loaded = load_research(bigquant_root, membership_zip, symbols=definition['symbols'],
        start=definition['window']['start'], end=definition['window']['end'])
    return loaded['inputs'], source_identities(loaded['inputs'], bigquant_root, membership_zip)


def inventory(selection, inputs, identities):
    definition = dict(selection)
    definition['raw_identities'] = identities
    definition['queries'] = {group: [] for group in GROUPS}
    for group in GROUPS:
        source = selection['routes'][group]
        result = inputs[group]
        if SOURCE_IDS[source] != result.metadata.source_id:
            raise ValueError('source registration conflicts with native reader identity')
        symbols = ([None] if group == 'calendar' else ['000905.SH'] if group == 'benchmark'
                   else selection['symbols'])
        for symbol in symbols:
            view = scoped(result, group, symbol, selection['window'])
            origins = {}
            if group == 'bars':
                origins = {prefix + field: 'stock.zip/stock/' + table + '/' + field
                    for prefix, table in [('raw_', 'cn_stock_real_bar1d'), ('adj_', 'cn_stock_bar1d')]
                    for field in ('open', 'high', 'low', 'close')}
            units = {name: ('BOOLEAN_0_1_OBSERVED_MAPPING' if group == 'status'
                            else 'UNKNOWN_VENDOR_CONTRACT') for name in NUMERIC.get(group, ())}
            definition['queries'][group].append(dict(
                query=asdict(HubQuery(source, view.metadata.data_version, KINDS[group],
                    selection['window']['start'], selection['window']['end'], symbol)),
                source_id=view.metadata.source_id, content_sha256=content_identity(view.data),
                units=units, field_origins=origins))
    return definition


def register(definition, inputs):
    sources, bindings, matrix = {}, {}, []
    for group, entries in definition['queries'].items():
        if group not in GROUPS:
            raise ValueError('unsupported input group: ' + group)
        bindings[group] = []
        for entry in entries:
            query = HubQuery(**entry['query'])
            source = definition['routes'][group]
            if query.source != source or query.kind != KINDS[group]:
                raise ValueError('source/kind differs from explicit route')
            result = inputs[group]
            if result.metadata.source_id != SOURCE_IDS[source]:
                raise ValueError('native source identity differs from registration')
            if (query.start, query.end) != (definition['window']['start'], definition['window']['end']):
                raise ValueError('query differs from fixed input window')
            view = scoped(result, group, query.symbol, definition['window'])
            key = (query.kind, query.symbol, query.start, query.end)
            routes = sources.setdefault(source, {})
            if key in routes:
                raise ValueError('duplicate route; no source fusion')
            routes[key] = view
            bindings[group].append(InputBinding(query, entry['source_id'], entry['content_sha256'],
                entry.get('units', {}), entry.get('field_origins', {})))
            frame = pd.DataFrame({'date': view.data}) if isinstance(view.data, pd.DatetimeIndex) else view.data
            keys = ['symbol'] if group == 'listings' else [c for c in ('date', 'symbol') if c in frame]
            dates = frame['date'] if 'date' in frame else pd.Series(dtype='datetime64[ns]')
            matrix.append(dict(group=group, source=source, source_id=view.metadata.source_id,
                version=view.metadata.data_version, kind=query.kind, symbol=query.symbol,
                rows=len(frame), fields=list(frame.columns), dtypes={c: str(t) for c, t in frame.dtypes.items()},
                missing_fields=sorted(set(FIELDS[group]) - set(frame)), units=entry.get('units', {}),
                undeclared_units=[c for c in NUMERIC.get(group, ())
                    if not entry.get('units', {}).get(c) or 'UNKNOWN' in entry['units'][c].upper()],
                first_date=None if dates.empty else str(dates.min().date()),
                last_date=None if dates.empty else str(dates.max().date()),
                unique_keys=not frame.duplicated(keys).any(),
                null_counts={c: int(n) for c, n in frame.isna().sum().items()},
                content_sha256=content_identity(view.data), known_at='UNKNOWN',
                announced_at='UNKNOWN', effective_at='listing_date/delist_date' if group == 'listings' else 'date',
                acquired_at=view.metadata.acquired_at,
                source_version=view.metadata.data_version,
                acquisition_role='archive export/run completion; NOT historical known_at',
                historical_eligibility='UNKNOWN'))
    return DataHub({source: ExactRoutes(routes) for source, routes in sources.items()}), bindings, matrix


def audit(definition, inputs, identities):
    if identities != definition['raw_identities']:
        raise ValueError('raw source identity drift; frozen input manifest must not be repinned')
    hub, bindings, matrix = register(definition, inputs)
    report = pipeline_preflight(hub, bindings, symbols=tuple(definition['symbols']),
        **definition['window'], require_actions=True, requested_formal=True)
    report.update(schema='P4_03_CONTROLLED_INPUTS_V1', status='PARTIAL',
        actual_raw_consumption='RUN', capabilities={s: sorted(k) for s, k in hub.capabilities().items()},
        input_matrix=matrix, raw_identities=identities,
        corporate_action_binding=dict(status='UNKNOWN', stable_security_mapping='MISSING',
            coverage='UNKNOWN; no event query registered; empty is not absence'),
        minimum_next_steps=[
            dict(category='SOURCE_CONTRACT', request='Confirm native price, volume, amount and turn economic units; do not infer units from values'),
            dict(category='IDENTITY_EVIDENCE', request='Bind stable security_id and full-window corporate-action source coverage before registering corporate_actions'),
            dict(category='COVERAGE', request='Resolve positive membership observations and listing-boundary warmup from existing evidence; do not request pre-listing prices'),
            dict(category='HISTORICAL_EVIDENCE', request='Obtain historical source vintages, first availability and independent HFQ qualification'),
        ])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--inventory', type=Path, help='selection JSON; emits candidate pins, not a preflight pass')
    mode.add_argument('--manifest', type=Path, help='frozen pins; never refreshed from current data')
    parser.add_argument('--bigquant-root', type=Path)
    parser.add_argument('--membership-zip', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--matrix', type=Path, help='optional aggregate input matrix CSV')
    args = parser.parse_args()
    path = args.inventory or args.manifest
    definition = json.loads(path.read_text(encoding='utf-8'))
    if bool(args.bigquant_root) != bool(args.membership_zip):
        parser.error('--bigquant-root and --membership-zip must be supplied together')
    if args.inventory and not args.bigquant_root:
        parser.error('inventory requires accessible controlled archive paths')
    if args.bigquant_root:
        inputs, identities = load(definition, args.bigquant_root, args.membership_zip)
        if args.inventory:
            dump(args.output, inventory(definition, inputs, identities))
            return 0
        report = audit(definition, inputs, identities)
    else:
        bindings = {g: tuple(InputBinding(HubQuery(**e['query']), e['source_id'], e['content_sha256'],
            e.get('units', {}), e.get('field_origins', {})) for e in entries)
            for g, entries in definition['queries'].items()}
        report = pipeline_preflight(DataHub({}), bindings, symbols=tuple(definition['symbols']),
            **definition['window'], require_actions=True, requested_formal=True)
        report.update(schema='P4_03_CONTROLLED_INPUTS_V1', status='PARTIAL', actual_raw_consumption='NOT_RUN',
            upstream_d07=d07_reference(ROOT / 'docs/validation/p3-d07/real-multisource.json'),
            raw_input_gap='Controlled originals not supplied; historical aggregates cannot satisfy six input queries')
    command = ['python', '-I', '-B', '-X', 'utf8', 'tools/data/audit_p4_03_controlled_inputs.py',
        '--manifest', path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else '<MANIFEST>',
        '--output', '<NEW_JSON>']
    if args.bigquant_root:
        command += ['--bigquant-root', '<BQ>', '--membership-zip', '<MEMBERSHIP>']
    if args.matrix:
        command += ['--matrix', '<NEW_CSV>']
    report['execution'] = dict(input_definition_sha256=sha256(path.read_bytes()),
        software_revision=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        implementation_sha256={name: sha256((ROOT / name).read_bytes()) for name in IMPLEMENTATION},
        python=platform.python_version(), pandas=pd.__version__, command=command)
    if args.matrix and report['actual_raw_consumption'] == 'RUN':
        with args.matrix.open('x', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=list(report['input_matrix'][0]))
            writer.writeheader()
            writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                for k, v in row.items()} for row in report['input_matrix'])
    dump(args.output, report)
    print(report['summary_zh'])
    return 1 if report['engineering'] != 'PASS' or report['formal_request'] == 'REJECTED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
