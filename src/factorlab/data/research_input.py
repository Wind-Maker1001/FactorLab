"""Pinned retrospective raw-price inputs; no historical knowledge admission."""
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
import time
import subprocess

import numpy as np
import pandas as pd

from factorlab.data.evidence import sha256, utc_now
from factorlab.data.hub.contracts import QueryResult, SourceMetadata, UnsupportedQueryError
from factorlab.dataset.identity import file_hash
from factorlab.errors import SchemaError

SAFETY = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
FIELDS = ['date', 'symbol', 'raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'trade_status', 'is_st']
PRICES = FIELDS[2:6]


def quality(bars, symbols, start, end):
    """Retain every observed key, and qualify only contiguous complete sessions."""
    if not set(FIELDS).issubset(bars):
        raise SchemaError('missing required raw execution fields')
    b = bars[FIELDS].copy()
    b['date'] = pd.to_datetime(b.date, errors='raise')
    if (b.empty or b.date.isna().any() or b.date.dt.tz is not None
            or not b.date.equals(b.date.dt.normalize())
            or b.duplicated(['date', 'symbol']).any()
            or not set(b.symbol).issubset(symbols)
            or not b.date.between(start, end).all()):
        raise SchemaError('invalid research input keys/scope')
    b = b.sort_values(['date', 'symbol']).reset_index(drop=True)
    numeric = b[PRICES + ['volume']].apply(pd.to_numeric, errors='coerce')
    finite = pd.Series(np.isfinite(numeric).all(axis=1), index=b.index)
    positive = numeric[PRICES].gt(0).all(axis=1) & numeric.volume.ge(0)
    ohlc = (numeric.raw_low.le(numeric[['raw_open', 'raw_close']].min(axis=1))
            & numeric.raw_high.ge(numeric[['raw_open', 'raw_close']].max(axis=1))
            & numeric.raw_low.le(numeric.raw_high))
    status = b.trade_status.isin([0, 1]) & b.is_st.isin([0, 1])
    ready = finite & positive & ohlc & status & b.trade_status.eq(1) & numeric.volume.gt(0)
    reasons = pd.Series('READY', index=b.index)
    reasons.loc[~ohlc | ~positive] = 'INVALID_OHLC_OR_VOLUME'
    reasons.loc[~finite] = 'MISSING_OR_NONFINITE_RAW_FIELDS'
    reasons.loc[b.trade_status.eq(0)] = 'SUSPENDED_OR_VENDOR_FILL_NO_CLOSE_MARK'
    reasons.loc[~status] = 'UNKNOWN_STATUS'
    reasons.loc[b.trade_status.eq(1) & numeric.volume.le(0)] = 'NORMAL_STATUS_WITHOUT_VOLUME'
    sessions = pd.DatetimeIndex(sorted(b.date.unique()))
    records = []
    for symbol in symbols:
        part = b.loc[b.symbol.eq(symbol)]
        mask = pd.Series(False, index=sessions)
        mask.loc[part.date] = ready.loc[part.index].to_numpy()
        runs, first = [], None
        for index, (day, valid) in enumerate(mask.items()):
            if valid and first is None:
                first = index
            if first is not None and (not valid or index == len(mask) - 1):
                last = index if valid else index - 1
                runs.append(dict(start=str(sessions[first].date()), end=str(sessions[last].date()), sessions=last-first+1))
                first = None
        missing = len(sessions.difference(part.date))
        accepted = int(ready.loc[part.index].sum())
        records.append(dict(symbol=symbol, status='RESEARCH_INPUT_READY' if accepted == len(sessions) else ('PARTIAL' if accepted else 'BLOCKED'),
            observed_rows=len(part), ready_rows=accepted, missing_keys=missing,
            first_observed=None if part.empty else str(part.date.min().date()),
            last_observed=None if part.empty else str(part.date.max().date()),
            listing_boundary='OBSERVATION_ONLY; LISTING_AND_DELISTING_UNKNOWN',
            rejected=reasons.loc[part.index].loc[lambda x: x.ne('READY')].value_counts().to_dict(), intervals=runs))
    jumps = b.groupby('symbol', sort=False).raw_close.pct_change(fill_method=None).abs().gt(.2)
    return dict(securities=records, sessions=len(sessions), observed_rows=len(b), ready_rows=int(ready.sum()),
        rejected_rows=int((~ready).sum()), rejected_reasons=reasons.loc[~ready].value_counts().to_dict(),
        missing_keys=sum(r['missing_keys'] for r in records), possible_action_jump_rows=int(jumps.sum()),
        years={str(y): int(sum(sessions.year == y)) for y in sorted(set(sessions.year))})


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare(config_path, source_root, raw_root, output):
    """Bind an existing single normalized snapshot without duplicating its prices."""
    clock = time.perf_counter()
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    root, output = Path(source_root), Path(output)
    payload = (root / 'manifest.json').read_bytes()
    if sha256(payload) != config['source_manifest_sha256']:
        raise SchemaError('source manifest identity mismatch')
    original = json.loads(payload)
    if original['schema'] != 'P5_R03_CSMAR_PRIVATE_V1':
        raise SchemaError('unsupported prepared source')
    data = (root / 'bars.parquet').read_bytes()
    if sha256(data) != original['bars_sha256']:
        raise SchemaError('consumed bars identity mismatch')
    checked = {}
    for ref in original['references']:
        name = ref['source_name']
        if Path(name).name != name:
            raise SchemaError('source basename required')
        if name not in checked:
            path = Path(raw_root) / name
            checked[name] = dict(sha256=file_hash(path), bytes=path.stat().st_size)
        if checked[name] != dict(sha256=ref['source_sha256'], bytes=ref['source_bytes']):
            raise SchemaError('original source identity mismatch')
    bars = pd.read_parquet(BytesIO(data))
    report = quality(bars, config['symbols'], config['start'], config['end'])
    output.mkdir(parents=True, exist_ok=False)
    _json(output / 'config.json', config)
    _json(output / 'quality.json', report)
    meta = SourceMetadata(source_id='CSMAR_TRDNEW_Dalyr_CANDIDATE', data_version='sha256:' + original['bars_sha256'],
        calendar_kind='OBSERVED_SECURITY_SESSIONS_ONLY', coverage='FIXED_24_RETROSPECTIVE; NO_FILL',
        provenance=dict(references=original['references'], original_sources_verified=checked,
            supplier_version='UNKNOWN', historical_first_available='UNKNOWN', revision_chain='UNKNOWN',
            corporate_actions='UNKNOWN; PRICE_JUMPS_ARE_CANDIDATES_ONLY', historical_membership='UNKNOWN',
            adjusted_price='adj_close=Adjprcwd; COMPARABLE_REINVESTMENT_CLOSE; NOT_CONSUMED'),
        field_semantics={**{c: dict(unit='CNY/share', native_field=n) for c,n in zip(PRICES,['Opnprc','Hiprc','Loprc','Clsprc'])},
            'volume': dict(unit='shares', native_field='Dnshrtrd'),
            'trade_status': dict(mapping='volume>0 AND Filling=0; Filling=2 excluded upstream; 0=nontrading'),
            'is_st': dict(mapping='Trdsta in {2,3,5,6,8,9,11,12,14,15}'),
            'precision': dict(value='native reviewed decimal conversion to float64; no new rounding')})
    full_securities = sum(r['status'] == 'RESEARCH_INPUT_READY' for r in report['securities'])
    status = ('P6_D01_RESEARCH_INPUT_READY' if full_securities >= 12 else
              ('P6_D01_PARTIAL' if report['ready_rows'] else 'P6_D01_BLOCKED'))
    manifest = dict(schema='P6_D01_RESEARCH_INPUT_V1', status=status,
        safety=SAFETY, formal_admission=False, historical_pit=False, hfq_verified=False,
        source_manifest_sha256=config['source_manifest_sha256'], bars_sha256=original['bars_sha256'],
        snapshot='EXTERNAL_IMMUTABLE_P5_R03_BARS; NO_DUPLICATE_PARQUET', metadata=asdict(meta),
        quality_sha256=file_hash(output / 'quality.json'), config_sha256=file_hash(output / 'config.json'),
        mapping_version='CSMAR_MARKET_CANDIDATE_V1_RAW_ONLY',
        code_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).resolve().parents[3], text=True).strip(),
        implementation_sha256=file_hash(Path(__file__)),
        implementation_files={str(p.relative_to(Path(__file__).resolve().parents[3])).replace('\\', '/'): file_hash(p)
            for p in (Path(__file__), Path(__file__).parent / 'csmar/prepare.py', Path(__file__).parent / 'csmar/profiles.py', Path(__file__).parent / 'hub/contracts.py')},
        prepared_at=utc_now(),
        consumed_bytes=len(data), elapsed_seconds=time.perf_counter()-clock)
    _json(output / 'manifest.json', manifest)
    return manifest, report


class ResearchInputAdapter:
    """Expose only explicitly qualified intervals, never a PIT query or filled mark."""
    capabilities = frozenset({'daily_bars', 'security_status', 'observed_sessions'})

    def __init__(self, root, source_root, expected_manifest):
        root = Path(root)
        payload = (root / 'manifest.json').read_bytes()
        if sha256(payload) != expected_manifest:
            raise SchemaError('research manifest identity mismatch')
        self.manifest = json.loads(payload)
        if self.manifest['schema'] != 'P6_D01_RESEARCH_INPUT_V1' or self.manifest['safety'] != SAFETY or any(self.manifest[k] for k in ('formal_admission','historical_pit','hfq_verified')):
            raise SchemaError('research-only contract required')
        for name in ('config', 'quality'):
            payload = (root / (name + '.json')).read_bytes()
            if sha256(payload) != self.manifest[name + '_sha256']:
                raise SchemaError(name + ' identity mismatch')
            setattr(self, name, json.loads(payload))
        source_root = Path(source_root)
        if file_hash(source_root / 'manifest.json') != self.manifest['source_manifest_sha256']:
            raise SchemaError('source manifest identity mismatch')
        payload = (source_root / 'bars.parquet').read_bytes()
        if sha256(payload) != self.manifest['bars_sha256']:
            raise SchemaError('bars identity mismatch')
        self.bars = pd.read_parquet(BytesIO(payload))[FIELDS]
        if quality(self.bars, self.config['symbols'], self.config['start'], self.config['end']) != self.quality:
            raise SchemaError('quality replay mismatch')

    def read(self, kind, *, symbol, security_id, start, end, knowledge_as_of):
        if knowledge_as_of is not None:
            raise UnsupportedQueryError('historical PIT unavailable')
        if kind not in self.capabilities or security_id is not None:
            raise UnsupportedQueryError('unsupported research query')
        entry = next((r for r in self.quality['securities'] if r['symbol'] == symbol), None)
        if entry is None or not any(r['start'] <= start <= end <= r['end'] for r in entry['intervals']):
            raise UnsupportedQueryError('window crosses rejected or missing execution sessions')
        frame = self.bars.loc[self.bars.symbol.eq(symbol) & self.bars.date.between(start, end)].copy()
        if kind == 'observed_sessions':
            frame = pd.DatetimeIndex(frame.date)
        elif kind == 'security_status':
            frame = frame[['date', 'symbol', 'trade_status', 'is_st']]
        return QueryResult(frame, SourceMetadata(**self.manifest['metadata']))
