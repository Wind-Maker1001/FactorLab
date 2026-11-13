"""Independent exact-ID evidence; never changes local order state or grants writes."""
from .protocol import Environment, ExchangeError


def exact_demo_order(client, client_order_id):
    """Query once, preserving absence as scoped evidence rather than an order event."""
    import re
    if client.environment is not Environment.DEMO:
        raise ValueError('Demo evidence requires Demo client')
    if not isinstance(client_order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,36}', client_order_id):
        raise ValueError('exact client order ID required')
    try:
        row = client.request('GET', '/fapi/v1/order',
                             {'symbol': 'NVDAUSDT', 'origClientOrderId': client_order_id})
    except ExchangeError as error:
        return dict(client_order_id=client_order_id, state='ABSENT' if error.code == -2013 else 'UNKNOWN',
                    http_status=error.status, code=error.code, message=error.message, category=error.category)
    if (not isinstance(row, dict) or row.get('symbol') != 'NVDAUSDT'
            or row.get('clientOrderId') != client_order_id):
        return dict(client_order_id=client_order_id, state='UNKNOWN', reason='RESPONSE_IDENTITY_MISMATCH')
    return dict(client_order_id=client_order_id, state='FOUND', http_status=200, response=row)
