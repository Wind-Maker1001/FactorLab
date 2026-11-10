"""Binance stock-spot market data; no trading or account operations."""

BOUNDARIES = ('NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED')
SCHEMA_VERSION = 1
SOURCE_VERSION = 'binance-stocks-openapi-1.0.0@f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179'
REST_HOST = 'https://api.binance.com'
WS_HOST = 'wss://nbstream.binance.com/equity'
INTERVALS = ('5m', '1h', '1d', '1w', '1M')
PHASES = {'C': 'CLOSED', 'ON': 'OVERNIGHT', 'PRE': 'PRE_MARKET',
          'O': 'REGULAR', 'POST': 'POST_MARKET'}
