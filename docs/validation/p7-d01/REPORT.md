# P7-D01 — isolated Binance production and official Demo reads

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## Observed access

The authorized bounded run began at 2026-11-12T06:42:18.185011+00:00.
Both credential pairs were entered through a local masked dialog and retained
only in the verification process's memory. The run made 13 GET requests, each
with a ten-second timeout, without automatic retries or host fallback.

| Endpoint | Production `fapi.binance.com` | Official Demo `demo-fapi.binance.com` |
| --- | --- | --- |
| `/fapi/v1/time` | HTTP 200 | HTTP 200 |
| `/fapi/v1/exchangeInfo` | HTTP 200 | HTTP 200 |
| `/fapi/v3/account` | HTTP 200 | HTTP 200 |
| `/fapi/v3/balance` | HTTP 200 | HTTP 200 |
| `/fapi/v3/positionRisk` | HTTP 200 | HTTP 200 |
| `/fapi/v1/openOrders` | HTTP 200 | HTTP 200 |

Production Stocks `GET https://api.binance.com/sapi/v1/equity/market/exchangeInfo?symbol=NVDA`
also returned HTTP 200 using the production read-only credential. Demo credentials
were not sent to the Stocks host. Only read capability outcomes were retained;
no balances, positions, orders, credentials, signed URLs or private account
response bodies were emitted or persisted. No order-write request was sent.

## Demo instrument discovery

The public Demo exchangeInfo response returned **743** symbols:

| Reported status | Count |
| --- | ---: |
| TRADING | 604 |
| SETTLING | 62 |
| PENDING_TRADING | 64 |
| PRE_SETTLE | 1 |
| DELIVERING | 12 |

**NVDAUSDT exists**, with `status=TRADING` and
`contractType=TRADIFI_PERPETUAL`. Among the other returned TRADING instruments
are BTCUSDT, ETHUSDT, TSLAUSDT, AMZNUSDT, METAUSDT, GOOGLUSDT, SPYUSDT and QQQUSDT.
The full public symbol/status/contract-type inventory and endpoint outcomes are
in [readonly-capabilities.json](readonly-capabilities.json). TRADING describes
exchangeInfo status; it is not proof of this account's eligibility to place an
order in that symbol.

## Isolation and validation

Network credentials require an explicit matching environment. The fixed immutable
transport refuses cross-environment requests, redirects and network mutations.
Missing Demo credentials cannot select production or legacy credentials. Adapter
store identity binds environment, account alias and API-key identity digest;
legacy offline identity is not promoted to an online account. TradingController
remains paper-only, and the read verifier does not open a paper ledger.

The official Demo host and v3 balance/account paths were checked against Binance's
[official SDK](https://github.com/binance/binance-connector-python/tree/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179),
including `common/src/binance_common/constants.py` and the USD-M account API.

Targeted verification: **80 passed** using:

```console
python -m pytest tests/integrations/test_binance_environments.py tests/integrations/test_binance_futures_trading.py tests/integrations/test_binance_equity.py -q
```

## Remaining gaps

- Demo order placement, cancellation and fills remain untested and blocked by the
  network write gate. Read success is not a trading-permission test.
- The order observation covers openOrders, not historical orders or a known-order
  query. No existing order identifier was exposed or required.
- Stocks rules access does not establish funds, holdings, sellable quantity,
  settlement, account-specific payment assets or trading eligibility.
- Personalized Futures leverage brackets and symbol configuration were not part
  of this bounded run. No account configuration or agreement was changed.
- All formal financial admission blocks remain in force; Demo/public rule access
  does not validate historical point-in-time or adjustment evidence.
