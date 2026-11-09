"""Bounded adapter for existing D06 archive readers; no alternative data store."""
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pandas as pd

from audit_p3_d06 import Archive, normalize, OHLC, STATUS
from candidate_preflight import SelectedTableAdapter
from factorlab.data.evidence import json_bytes, sha256, table_sha256
from factorlab.data.hub.contracts import QueryResult, SourceMetadata


def load_research(root, membership_zip, *, symbols, start, end):
    """Reuse D06 mappings, rejecting every consumed partition receipt mismatch."""
    manifest = []
    inputs = {}
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)

    def read(category, table, columns, selected_symbols):
        archive = Archive(Path(root), category, manifest)
        begin = len(manifest)
        try:
            frame = archive.read(table, columns, lo, hi, selected_symbols)
        finally:
            archive.zip.close()
        entries = manifest[begin:]
        if any(not row['hash_matches_receipt'] for row in entries):
            raise ValueError('D06 research partition differs from receipt: ' + table)
        # Only selected partitions define this version, not today's whole archive.
        return frame, entries

    def result(kind, frame, entries, source='BigQuant'):
        content = table_sha256(frame)
        version = 'sha256:' + sha256(json_bytes(entries))
        inputs[kind] = QueryResult(frame, SourceMetadata(source, version,
            provenance=dict(source_kind='REAL_CANDIDATE', table_sha256=content,
                input_members=entries, historical_version='UNKNOWN',
                mapping='D06_OBSERVED_SUPPLIER_CANDIDATE_NOT_CERTIFIED')))

    raw, raw_refs = read('stock', 'cn_stock_real_bar1d',
        ['date', 'instrument', *OHLC, 'volume', 'amount', 'turn'], set(symbols))
    adjusted, adj_refs = read('stock', 'cn_stock_bar1d', ['date', 'instrument', *OHLC], set(symbols))
    bars = raw.rename(columns={**{f: 'raw_' + f for f in OHLC}, 'turn': 'turnover'}).merge(
        adjusted.rename(columns={f: 'adj_' + f for f in OHLC}), on=['date', 'symbol'],
        how='outer', validate='one_to_one')
    result('bars', bars, raw_refs + adj_refs)
    dates, refs = read('common', 'trading_days', ['date', 'market_code'], None)
    result('calendar', dates.loc[dates.market_code.eq('CN'), ['date']].drop_duplicates().sort_values('date'), refs)
    benchmark, refs = read('index', 'cn_stock_index_bar1d', ['date', 'instrument', 'open'], {'000905.SH'})
    result('benchmark', benchmark, refs)
    status, refs = read('stock', 'cn_stock_status', ['date', 'instrument', *STATUS], set(symbols))
    # Missing source states remain missing; never turn a null status into healthy.
    mapped = status[['date', 'symbol']].copy()
    mapped['trade_status'] = status.suspended.eq(0).astype(float).where(status.suspended.notna())
    mapped['is_st'] = status.st_status.ne(0).where(status.st_status.notna())
    result('status', mapped, refs)
    listings, refs = read('stock', 'cn_stock_basic_info', ['instrument', 'list_date', 'delist_date'], set(symbols))
    result('listings', listings.rename(columns={'list_date': 'listing_date'}), refs)
    with zipfile.ZipFile(membership_zip) as archive:
        payload = archive.read('members_daily.csv')
        receipt = archive.read('manifest.json')
    members = pd.read_csv(BytesIO(payload), encoding='utf-8-sig')
    members['date'] = pd.to_datetime(members.date)
    members['symbol'] = normalize(members.stock_code)
    members = members.loc[members.date.between(lo, hi) & members.symbol.isin(symbols), ['date', 'symbol']]
    result('membership', members, dict(member_sha256=sha256(payload), manifest_sha256=sha256(receipt)),
        source='JoinQuant_OBSERVED_MEMBERSHIP')
    identities = {kind: (value.metadata.source_id, value.metadata.data_version) for kind, value in inputs.items()}
    adapter = SelectedTableAdapter(bars, inputs['bars'].metadata)
    return adapter, dict(inputs=inputs, identities=identities)
