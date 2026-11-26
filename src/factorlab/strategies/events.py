"""Causal multi-symbol observation envelope for the V2 decision boundary."""
from factorlab.trading.contracts import utc
from .protocol import V2


class StrategyInputUnavailable(ValueError):
    """A declared input is unavailable at this decision time."""


class ReferenceUnavailable(StrategyInputUnavailable):
    """A declared read-only series has no complete causal observation window."""


def strategy_event(event, spec):
    """Project requested fields for Python without changing the execution envelope."""
    result = dict(event)
    for key in ('account', 'allocation'):
        fields = spec.data.get(key + '_fields')
        if fields is not None:
            if not set(fields) <= event[key].keys():
                raise StrategyInputUnavailable('missing declared ' + key + ' fields')
            result[key] = {field: event[key][field] for field in fields}
    return result


def validate_event(event, spec):
    if event['protocol'] != V2:
        raise ValueError('V2 event required')
    spec.require_binding(event['binding'])
    if not isinstance(event['event_id'], str) or not event['event_id']:
        raise ValueError('durable event identity required')
    utc(event['event_time'])
    if event['data_type'] not in spec.data['data_types']:
        raise ValueError('undeclared event type')
    if (not isinstance(event['account'], dict) or not isinstance(event['allocation'], dict)
            or not isinstance(event['symbols'], dict) or not event['symbols']
            or not set(event['symbols']) <= set(spec.symbols)):
        raise ValueError('invalid event projections/symbols')
    if 'context' in event and not isinstance(event['context'], dict):
        raise ValueError('adapter context must be a data object')
    strategy_event(event, spec)
    for context in event['symbols'].values():
        if not isinstance(context['watermark'], str) or not context['watermark']:
            raise ValueError('symbol watermark required')
        for data_type, observation in context['observations'].items():
            _observation(observation, data_type, event, spec.data['data_types'])
        for data_type, history in context['history'].items():
            if not isinstance(history, list) or len(history) > spec.data['history_limit']:
                raise ValueError('history exceeds declared requirement')
            for observation in history:
                _observation(observation, data_type, event, spec.data['data_types'])
    _references(event, spec.data.get('references', {}))
    return event


def _references(event, declared):
    references = event.get('references', {})
    if not references and event['data_type'] != 'closed_bar':
        return
    if set(references) != set(declared):
        raise ReferenceUnavailable('reference symbols differ from the declaration')
    for symbol, streams in declared.items():
        context = references[symbol]
        if not isinstance(context['watermark'], str) or not context['watermark']:
            raise ValueError('reference watermark required')
        if set(context['history']) != set(streams) or set(context['observations']) != set(streams):
            raise ReferenceUnavailable('reference observation types differ from the declaration: ' + symbol)
        for kind, requirement in streams.items():
            history = context['history'][kind]
            if (not isinstance(history, list) or len(history) != requirement['history_limit']
                    or context['observations'][kind] != history[-1]):
                raise ReferenceUnavailable('incomplete reference history: ' + symbol + '/' + kind)
            for observation in history:
                _observation(observation, kind, event, streams)
                payload = observation['payload']
                if observation['source'] != requirement['source']:
                    raise ValueError('reference source mismatch: ' + symbol)
                if kind != 'funding_forecast' and observation['observed_at'] < observation['effective_at']:
                    raise ValueError('reference observation precedes its effective time: ' + symbol)
                if kind == 'closed_bar' and (payload['price_basis'] != requirement['price_basis']
                        or payload['interval_seconds'] != requirement['interval_seconds']):
                    raise ValueError('reference bar provenance mismatch: ' + symbol)
            times = [(entry['observed_at'], entry['effective_at']) for entry in history]
            if times != sorted(set(times)):
                raise ValueError('reference history must be ordered and unique: ' + symbol)


def _observation(observation, data_type, event, declared):
    if data_type not in declared:
        raise ValueError('undeclared observation type')
    utc(observation['observed_at'])
    utc(observation['effective_at'])
    if (observation['observed_at'] > event['event_time']
            or data_type != 'funding_forecast' and observation['effective_at'] > event['event_time']):
        raise ValueError('noncausal observation')
    if (not isinstance(observation['source'], str) or not observation['source']
            or not isinstance(observation['payload'], dict)):
        raise ValueError('observation source and payload required')
