"""Project versioned DataHub observations into the read-only V2 event envelope."""
from datetime import datetime

from factorlab.trading.contracts import utc

from .events import ReferenceUnavailable
from .identity import content_identity


class DataHubReferences:
    """Source adapters expose `strategy_<kind>` with explicit knowledge times."""

    def __init__(self, hub):
        self.hub = hub

    def supports(self, symbol, kind, requirement):
        return 'strategy_' + kind in self.hub.capabilities().get(requirement['source'], ())

    def history(self, symbol, kind, requirement, *, start, end, as_of):
        from factorlab.errors import DataQualityError

        utc(as_of)
        source = requirement['source']
        try:
            result = self.hub.read(source, 'strategy_' + kind, symbol=symbol, start=start, end=end,
                                   knowledge_as_of=as_of.isoformat())
        except DataQualityError as error:
            raise ReferenceUnavailable(f'{source}/{symbol}/{kind}: {error}') from error
        metadata = result.metadata
        if metadata.source_id != source or not metadata.data_version:
            raise ReferenceUnavailable('reference result requires exact source and data version')
        observations = []
        columns = ['symbol', 'observed_at', 'effective_at', 'payload']
        if not hasattr(result.data, 'columns') or not set(columns) <= set(result.data.columns):
            raise ReferenceUnavailable('reference result is missing observation fields')
        for row in result.data[columns].to_dict('records'):
            observed = _time(row['observed_at'])
            effective = _time(row['effective_at'])
            if (row['symbol'] != symbol or observed > as_of
                    or kind != 'funding_forecast' and effective > as_of):
                raise ReferenceUnavailable('reference result exceeds symbol or knowledge cutoff')
            observations.append(dict(source=source, source_version=metadata.data_version,
                                     observed_at=observed, effective_at=effective, payload=row['payload']))
        observations.sort(key=lambda item: (item['observed_at'], item['effective_at']))
        return observations[-requirement['history_limit']:]


def _time(value):
    time = datetime.fromisoformat(value) if isinstance(value, str) else value
    utc(time)
    return time


def reference_context(streams):
    """Keep the same current/history projection as the trading-symbol envelope."""
    if any(not rows for rows in streams.values()):
        raise ReferenceUnavailable('empty reference history')
    return dict(watermark=content_identity(streams),
                observations={kind: rows[-1] for kind, rows in streams.items()}, history=streams)
