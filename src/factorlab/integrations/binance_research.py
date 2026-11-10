"""Pinned, offline Binance research queries; no financial admission or FX conversion."""
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from factorlab.data.hub.contracts import QueryResult, UnsupportedQueryError
from factorlab.errors import DataQualityError
from factorlab.integrations.binance.market import INTERVALS, utc
from factorlab.integrations.binance.store import Reader as PerpetualReader
from factorlab.integrations.binance_equity.store import Reader as EquityReader

SOURCES = {'BINANCE_USDM_PUBLIC_REST': (PerpetualReader, ('bars', 'funding', 'mark')),
           'BINANCE_EQUITY': (EquityReader, ('symbols', 'quotes', 'prices', 'bars',
                                               'open_bars', 'sessions', 'gaps'))}


def _instant(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise UnsupportedQueryError('explicitly zoned timestamp required')
    return utc(value)


def _times(values, *, epoch=False):
    if epoch:
        return pd.to_datetime(values, unit='ms', utc=True)
    return pd.Series([_instant(v) for v in values], index=values.index,
                     dtype='datetime64[ns, UTC]')


def _require(frame, columns):
    missing = set(columns) - set(frame.columns)
    if missing:
        raise DataQualityError('missing research fields: ' + ', '.join(sorted(missing)))


def _numbers(frame, kind):
    fields = {'bars': ('open', 'high', 'low', 'close', 'volume'),
              'open_bars': ('open', 'high', 'low', 'close', 'volume'),
              'quotes': ('bid_price', 'ask_price', 'bid_size', 'ask_size'),
              'prices': ('price',), 'funding': ('funding_rate',),
              'mark': ('mark_price',)}.get(kind, ())
    _require(frame, fields)
    optional = ('quote_volume', 'taker_buy_volume', 'taker_buy_quote_volume',
                'mark_price', 'index_price', 'estimated_settle_price',
                'last_funding_rate', 'interest_rate', 'vwap',
                'previous_regular_close', 'today_regular_close')
    for field in dict.fromkeys((*fields, *optional)):
        if field not in frame:
            continue
        for value in frame[field]:
            if pd.isna(value):
                if field in fields:
                    raise DataQualityError('unavailable required numeric field: ' + field)
                continue
            if not isinstance(value, (str, Decimal)):
                raise DataQualityError('expected exact decimal text: ' + field)
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise DataQualityError('invalid decimal: ' + field) from exc
            signed = field in ('funding_rate', 'last_funding_rate', 'interest_rate')
            if not number.is_finite() or (not signed and number < 0):
                raise DataQualityError('nonfinite or negative numeric field: ' + field)
    if kind in ('bars', 'open_bars'):
        for row in frame.itertuples():
            o, h, l, c = map(Decimal, (row.open, row.high, row.low, row.close))
            if not l <= min(o, c) <= max(o, c) <= h:
                raise DataQualityError('inconsistent OHLC range')


class BinanceResearch:
    """One immutable generation. Queries require an explicit observation cutoff.

    Event windows are [start, end). Untimestamped observations must explicitly
    use time_basis='acquired_at'. That is not historical market availability.
    mark is accessible only with purpose='snapshot', never as a price history.
    Source-level Equity sessions/gaps require instrument_id=None.
    """

    def __init__(self, source_id, snapshot, *, expected_data_version):
        if source_id not in SOURCES:
            raise UnsupportedQueryError('unknown Binance research source')
        root = Path(snapshot).resolve()
        if not (root / 'manifest.json').is_file() or (root / 'latest.json').exists():
            raise UnsupportedQueryError('pin a snapshot generation, not a latest pointer')
        self.source_id = source_id
        self.root = root
        self.expected_data_version = expected_data_version

    def read(self, kind, *, instrument_id, start, end, observation_cutoff,
             time_basis='event_time', purpose='research'):
        """Return native exact values and UTC research times with source evidence."""
        reader_type, tables = SOURCES[self.source_id]
        if kind not in tables or time_basis not in ('event_time', 'acquired_at'):
            raise UnsupportedQueryError('unsupported dataset or time basis')
        if purpose not in ('research', 'snapshot') or (kind == 'mark' and purpose != 'snapshot'):
            raise UnsupportedQueryError('mark is a snapshot, not historical valuation')
        start, end, cutoff = map(_instant, (start, end, observation_cutoff))
        if start >= end:
            raise UnsupportedQueryError('require start < end (exclusive)')
        reader = reader_type(self.root)
        manifest = reader.manifest
        if manifest['data_version'] != self.expected_data_version:
            raise DataQualityError('pinned data version differs')
        raw_count = reader.verify_raw()
        if self.source_id == 'BINANCE_USDM_PUBLIC_REST' and kind in ('bars', 'funding') and time_basis == 'event_time':
            bounds = manifest['range']
            if (start < pd.Timestamp(bounds['start_ms'], unit='ms', tz='UTC') or
                    end > pd.Timestamp(bounds['end_ms'], unit='ms', tz='UTC')):
                raise UnsupportedQueryError('requested time range exceeds snapshot coverage')
            if kind == 'bars':
                for gap in manifest['integrity']['missing_ranges']:
                    if _instant(gap['start']) < end and _instant(gap['end_exclusive']) > start:
                        raise UnsupportedQueryError('requested closed bars contain missing coverage')
        result = reader.read(kind)
        frame = result.data.copy()
        equity = self.source_id == 'BINANCE_EQUITY'
        source_level = equity and kind in ('sessions', 'gaps')
        if source_level != (instrument_id is None):
            raise UnsupportedQueryError('instrument identity required only for instrument datasets')
        query = dict(kind=kind, instrument_id=instrument_id, start=start.isoformat(),
                     end_exclusive=end.isoformat(), observation_cutoff=cutoff.isoformat(),
                     time_basis=time_basis, purpose=purpose)
        if equity:
            if not source_level:
                prefix = 'BINANCE:EQUITY:'
                if not isinstance(instrument_id, str) or not instrument_id.startswith(prefix):
                    raise UnsupportedQueryError('expected stock spot instrument identity')
                ticker = instrument_id[len(prefix):]
                from factorlab.integrations.binance_equity.client import symbol
                symbol(ticker)
                if not frame.empty:
                    _require(frame, ('symbol', 'asset_class', 'currency'))
                    if not frame.asset_class.eq('STOCK_SPOT').all() or not frame.currency.eq('USD').all():
                        raise DataQualityError('Equity asset class or currency conflict')
                    frame = frame.loc[frame.symbol.eq(ticker)].copy()
            instrument_type = 'STOCK_SPOT' if not source_level else None
        else:
            contract = manifest['contract']
            from factorlab.integrations.binance.market import identity
            native = identity(contract['exchange_info'])
            if any(contract[k] != native[k] for k in
                   ('instrument_id', 'symbol', 'contract_type', 'quote_asset', 'margin_asset')):
                raise DataQualityError('perpetual manifest contract identity conflict')
            if instrument_id != native['instrument_id']:
                raise UnsupportedQueryError('requested contract differs from snapshot')
            instrument_type = contract['contract_type']
            if instrument_type not in ('TRADFI_PERPETUAL', 'TRADIFI_PERPETUAL'):
                raise DataQualityError('unsupported native contract type')
        if frame.empty:
            reason = ('EQUITY_CLOSED_BAR_RESEARCH_NOT_AVAILABLE' if equity and kind == 'bars'
                      else 'REQUESTED_DATA_NOT_AVAILABLE')
            raise UnsupportedQueryError(reason)
        if equity and kind == 'gaps':
            if time_basis != 'acquired_at':
                raise UnsupportedQueryError('gaps have no market event time; query acquired_at')
            frame['acquired_at'] = manifest['acquired_at']
        _require(frame, ('acquired_at',) if equity else ())
        if equity:
            if 'quantity_unit' in frame and not frame.quantity_unit.eq('shares').all():
                raise DataQualityError('Equity quantity unit conflict')
            frame['native_event_time'] = frame['event_time'] if 'event_time' in frame else pd.NA
            frame['acquired_at'] = _times(frame.acquired_at)
            if 'event_time' in frame:
                frame['event_time'] = _times(frame.event_time, epoch=True)
            else:
                frame['event_time'] = pd.Series(pd.NaT, index=frame.index, dtype='datetime64[ns, UTC]')
            if 'server_event_time' in frame:
                frame['native_server_event_time'] = frame.server_event_time
                frame['server_event_time'] = _times(frame.server_event_time, epoch=True)
            for column in ('open_time', 'close_time'):
                if column in frame:
                    frame['native_' + column] = frame[column]
                    frame[column] = _times(frame[column], epoch=True)
            if 'raw_receipt' in frame:
                known = {Path(d['path']).stem for d in manifest['raw_receipts']}
                if not frame.raw_receipt.isin(known).all():
                    raise DataQualityError('row references unknown raw receipt')
            elif not source_level:
                raise DataQualityError('missing row raw receipt identity')
        else:
            frame['acquired_at'] = _instant(manifest['acquired_at'])
            event = {'bars': 'open_time', 'funding': 'funding_time', 'mark': 'event_time'}[kind]
            _require(frame, (event,))
            frame['event_time'] = _times(frame[event])
            frame['quote_asset'] = contract['quote_asset']
            frame['margin_asset'] = contract['margin_asset']
            if kind in ('bars', 'funding'):
                bounds = manifest['range']
                events = frame.event_time
                if not ((events >= pd.Timestamp(bounds['start_ms'], unit='ms', tz='UTC')) &
                        (events < pd.Timestamp(bounds['end_ms'], unit='ms', tz='UTC'))).all():
                    raise DataQualityError('records outside snapshot coverage')
        if kind in ('bars', 'open_bars'):
            _require(frame, ('open_time', 'close_time'))
            if equity:
                frame['server_event_time'] = frame.event_time
                frame['event_time'] = frame.open_time
            if frame[['open_time', 'close_time']].isna().any().any():
                raise DataQualityError('missing candle boundary')
            if (frame.close_time < frame.open_time).any():
                raise DataQualityError('candle boundary conflict')
            if equity:
                _require(frame, ('closed', 'interval'))
                if not frame.closed.map(lambda v: type(v) is bool and v == (kind == 'bars')).all():
                    raise DataQualityError('closed/open candle boundary conflict')
            else:
                step = INTERVALS[manifest['interval']] * 1_000_000
                if ((frame.open_time.astype('int64') % step != 0).any() or
                        (frame.close_time.astype('int64') != frame.open_time.astype('int64') + step - 1_000_000).any()):
                    raise DataQualityError('perpetual UTC candle boundary conflict')
            if kind == 'bars' and (frame.close_time >= frame.acquired_at).any():
                raise DataQualityError('closed candle observed before closure')
        frame = frame.loc[frame.acquired_at <= cutoff].copy()
        if frame.empty:
            raise UnsupportedQueryError('no observations available at observation cutoff')
        if kind == 'quotes' and 'availability' in frame and not frame.availability.eq('AVAILABLE').all():
            raise UnsupportedQueryError('source reported NO_QUOTE')
        _numbers(frame, kind)
        if kind == 'funding' and not frame.rate_type.dropna().isin(('Regular', 'Special')).all():
            raise DataQualityError('unknown funding rate type')
        if kind in ('bars', 'open_bars'):
            keys = ['open_time', 'interval'] if equity else ['open_time']
            versions = keys + ['acquired_at'] + (['server_event_time'] if equity else [])
            if frame.duplicated(versions).any():
                raise DataQualityError('duplicate or conflicting candle observation')
            frame = frame.sort_values(versions, kind='stable').drop_duplicates(keys, keep='last')
        else:
            keys = (['start', 'end', 'reason'] if kind == 'gaps' else
                    ['funding_time', 'rate_type'] if kind == 'funding' else
                    ['event_time', 'acquired_at'] + (['server_event_time'] if 'server_event_time' in frame else []))
            if frame.duplicated(keys).any():
                raise DataQualityError('duplicate or conflicting observation')
        if time_basis == 'event_time' and frame.event_time.isna().any():
            raise UnsupportedQueryError('event time unavailable; explicitly query acquired_at')
        times = frame[time_basis]
        frame = frame.loc[(times >= start) & (times < end)].copy()
        frame['instrument_id'] = instrument_id
        frame['instrument_type'] = instrument_type
        frame['source_id'] = self.source_id
        frame['data_type'] = kind
        frame['data_version'] = manifest['data_version']
        context = ({'observed_gaps': (reader.read('gaps').data.to_dict('records')
                                      if _instant(manifest['acquired_at']) <= cutoff else None),
                    'capabilities': (manifest['capabilities']
                                     if _instant(manifest['acquired_at']) <= cutoff else None),
                    'acquired_basis': 'manifest.acquired_at' if kind == 'gaps' else 'row.acquired_at'}
                   if equity else {'acquired_basis': 'manifest.acquired_at'})
        metadata = replace(result.metadata,
            evidence_status=tuple(dict.fromkeys((*result.metadata.evidence_status,
                                  'HISTORICAL_AVAILABILITY_UNVERIFIED'))),
            time_fields={**result.metadata.time_fields, 'research_event': 'event_time (UTC, nullable)',
                         'research_acquired': 'acquired_at (UTC)',
                         'observation_cutoff': cutoff.isoformat()},
            provenance={**result.metadata.provenance, **context, 'query': query,
                        'schema_version': manifest['schema_version'],
                        'verified_raw_receipts': raw_count,
                        'snapshot_tables': manifest['tables'],
                        'raw_evidence': manifest['raw_receipts'],
                        'empty_window': frame.empty,
                        'version_selection': 'latest observation <= explicit cutoff; not historical PIT'},
            field_semantics={**result.metadata.field_semantics,
                             'instrument_id': {'meaning': 'source-native; no inferred stock mapping'},
                             'mark': {'meaning': 'observed snapshot only; not historical valuation'}})
        return QueryResult(frame.reset_index(drop=True), metadata)
