"""Causal multi-symbol observation envelope for the V2 decision boundary."""
from factorlab.trading.contracts import utc
from .protocol import V2


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
    for context in event['symbols'].values():
        if not isinstance(context['watermark'], str) or not context['watermark']:
            raise ValueError('symbol watermark required')
        for data_type, observation in context['observations'].items():
            _observation(observation, data_type, event, spec)
        for data_type, history in context['history'].items():
            if not isinstance(history, list) or len(history) > spec.data['history_limit']:
                raise ValueError('history exceeds declared requirement')
            for observation in history:
                _observation(observation, data_type, event, spec)
    return event


def _observation(observation, data_type, event, spec):
    if data_type not in spec.data['data_types']:
        raise ValueError('undeclared observation type')
    utc(observation['observed_at'])
    utc(observation['effective_at'])
    if (observation['observed_at'] > event['event_time']
            or data_type != 'funding_forecast' and observation['effective_at'] > event['event_time']):
        raise ValueError('noncausal observation')
    if (not isinstance(observation['source'], str) or not observation['source']
            or not isinstance(observation['payload'], dict)):
        raise ValueError('observation source and payload required')
