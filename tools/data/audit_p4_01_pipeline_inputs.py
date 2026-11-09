"""Read pinned input definitions and optional existing D07 controlled archives."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from factorlab.application.research_pipeline import HubQuery
from factorlab.data.evidence import sha256
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import QueryResult, UnsupportedQueryError
from factorlab.diagnostics.pipeline_preflight import InputBinding, content_identity, pipeline_preflight

D07_RECEIPT = '64163b6c2ce3f797da1d5b49a5c2bb1b110f550bebbfa352df0860cd1917f0b5'


def d07_reference(path):
    payload = Path(path).read_bytes()
    if sha256(payload) != D07_RECEIPT:
        raise ValueError('fixed D07 multisource aggregate identity mismatch')
    report = json.loads(payload)
    return dict(schema=report['schema'], content_sha256=D07_RECEIPT,
        observations_origin='REUSED_UPSTREAM_RUN_NOT_RECOMPUTED',
        raw_consumption='NOT_RUN', historical_pit='UNKNOWN',
        sources=[dict(query=r['query'], accessibility=r['accessibility'],
                      metadata=r['source'], checks=r['checks']) for r in report['sources']],
        formal_admission='BLOCKED')


class _D07ResultAdapter:
    """Expose already verified D07 results as scoped Hub views; no acquisition or repair."""
    def __init__(self, results):
        self.results = results
        self.capabilities = frozenset(results)

    def read(self, kind, *, symbol, start, end, knowledge_as_of, **kwargs):
        if knowledge_as_of is not None:
            raise UnsupportedQueryError('D07 retrospective inputs have UNKNOWN historical knowledge')
        result = self.results[kind]
        frame = result.data
        if 'symbol' in frame:
            frame = frame.loc[frame.symbol.eq(symbol)]
        if 'date' in frame:
            frame = frame.loc[frame.date.between(start, end)]
        frame = frame.copy()
        provenance = dict(result.metadata.provenance)
        provenance['parent_research_table_sha256'] = provenance['research_table_sha256']
        provenance['research_table_sha256'] = content_identity(frame)
        data = pd.DatetimeIndex(frame.date) if list(frame.columns) == ['date'] else frame
        return QueryResult(data, replace(result.metadata, provenance=provenance))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--d07-receipt', type=Path, default=Path(__file__).resolve().parents[2] /
                        'docs/validation/p3-d07/real-multisource.json')
    parser.add_argument('--bigquant-root', type=Path)
    parser.add_argument('--membership-zip', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    definition = json.loads(args.manifest.read_text(encoding='utf-8'))
    reference = d07_reference(args.d07_receipt)
    bindings = {group: tuple(InputBinding(query=HubQuery(**item['query']),
        source_id=item['source_id'], content_sha256=item['content_sha256'],
        units=item.get('units', {}), field_origins=item.get('field_origins', {}))
        for item in entries) for group, entries in definition['queries'].items()}
    options = definition['window']
    sources = {}
    if bool(args.bigquant_root) != bool(args.membership_zip):
        parser.error('--bigquant-root and --membership-zip must be supplied together')
    if args.bigquant_root:
        from d06_readiness_adapter import load_research
        _, loaded = load_research(args.bigquant_root, args.membership_zip,
            symbols=definition['symbols'], start=options['start'], end=options['end'])
        for group, pins in bindings.items():
            for pin in pins:
                routed = sources.setdefault(pin.query.source, {})
                if pin.query.kind in routed and routed[pin.query.kind] is not loaded['inputs'][group]:
                    raise ValueError('ambiguous source/kind routing')
                routed[pin.query.kind] = loaded['inputs'][group]
    hub = DataHub({name: _D07ResultAdapter(results) for name, results in sources.items()})
    report = pipeline_preflight(hub, bindings, symbols=tuple(definition['symbols']), **options,
        requested_formal=definition.get('requested_formal', False),
        require_actions=definition.get('require_actions', True))
    report['input_definition_sha256'] = sha256(args.manifest.read_bytes())
    report['upstream_d07'] = reference
    report['actual_raw_consumption'] = 'RUN' if sources else 'NOT_RUN'
    if not sources:
        report['raw_input_gap'] = 'No controlled raw archive paths supplied; aggregate receipts cannot satisfy input queries'
    with args.output.open('x', encoding='utf-8') as target:
        json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        target.write('\n')
    print(report['summary_zh'])
    return 1 if report['engineering'] != 'PASS' or report['formal_request'] == 'REJECTED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
