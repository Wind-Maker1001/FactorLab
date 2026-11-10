"""Offline bounded evidence run: python examples/binance_tradfi_research.py --help."""
import argparse
import json
from pathlib import Path
import socket
import time
import tracemalloc

from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.integrations.binance_research import BinanceResearch


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--config', type=Path, default=Path('configs/binance_tradfi_research.json'))
    parser.add_argument('--equity-snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    def no_network(*args, **kwargs):
        raise RuntimeError('offline research: network forbidden')
    socket.socket.connect = no_network
    socket.create_connection = no_network
    tracemalloc.start()
    started = time.perf_counter()
    output = {'boundaries': ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED',
                             'FORMAL_ADMISSION_BLOCKED'], 'datasets': []}
    for spec in config['snapshots']:
        path = args.equity_snapshot if spec['source_id'] == 'BINANCE_EQUITY' else Path(spec['snapshot'])
        if path is None or not (path / 'manifest.json').is_file():
            output['datasets'].append({'source_id': spec['source_id'], 'status': 'NOT_RUN',
                                       'reason': 'SNAPSHOT_NOT_AVAILABLE'})
            continue
        bridge = BinanceResearch(spec['source_id'], path,
                                 expected_data_version=spec['data_version'])
        for query in spec['queries']:
            identity = dict(source_id=spec['source_id'], kind=query['kind'],
                            data_version=spec['data_version'])
            try:
                result = bridge.read(**query, instrument_id=spec['instrument_id'],
                                     observation_cutoff=config['observation_cutoff'])
            except UnsupportedQueryError as error:
                output['datasets'].append(dict(identity, status='UNAVAILABLE', reason=str(error)))
                continue
            frame = result.data
            output['datasets'].append(dict(identity, status='READ', rows=len(frame),
                instrument_ids=frame.instrument_id.dropna().unique().tolist(),
                event_start=None if frame.event_time.dropna().empty else frame.event_time.min().isoformat(),
                event_end=None if frame.event_time.dropna().empty else frame.event_time.max().isoformat(),
                null_event_times=int(frame.event_time.isna().sum()),
                acquired_start=frame.acquired_at.min().isoformat() if len(frame) else None,
                acquired_end=frame.acquired_at.max().isoformat() if len(frame) else None,
                raw_receipts=result.metadata.provenance['verified_raw_receipts'],
                table_evidence=result.metadata.provenance['snapshot_tables'],
                evidence_status=list(result.metadata.evidence_status)))
    output['elapsed_seconds'] = time.perf_counter() - started
    output['python_peak_allocated_bytes'] = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    body = json.dumps(output, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(body + '\n', encoding='utf-8')
    print(body)


if __name__ == '__main__':
    main()
