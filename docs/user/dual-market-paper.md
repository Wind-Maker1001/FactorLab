# Dual-market offline engineering exercise

Run the installed package with Python 3.14:

```sh
python -m factorlab.trading.dual_market
```

All quotes, calendar observations, trading rules, credentials and balances are
synthetic. The entry point constructs only FakeTransport; it never reads real
credentials or enables network execution. Temporary SQLite journals are removed
when the command exits. No historical data or supplier access is required.

The buy signal is an illustrative ask-price threshold, not a validated strategy.
NVDAUSDT perpetuals and NVDA stock are distinct identities. Futures start with
1000 USDT and stocks with 1000 USD; neither currency is converted or netted.

The futures order buys 2 units at 100, filled as 0.75 and 1.25. The local
controller restarts after the first booking, queries the existing protocol
identity and recovers the missing execution. Expected final wallet: 999.8 USDT,
collateral: 200 USDT, quantity: 2, fees: 0.2 USDT.

The stock order requests 0.5 shares at synthetic ask 150 with tokenize=false.
It fills 0.2 and cancels the rest. A sell reserves 0.1 shares, fills 0.05 and
cancels the remainder. Expected final cash: 977.4625 USD, unreserved inventory:
0.15 shares, fees: 0.0375 USD. The stock controller then restarts independently
and reconciles against the pre-restart paper authority. Actual settlement stays
UNKNOWN (`None` in the existing holdings API).

## Recovery policy

| Condition | New submission | Required action / ledger result |
| --- | --- | --- |
| Protocol acceptance, local transaction interrupted | Blocked by protocol/paper identity mismatch | Query and import the already accepted identity; never POST a replacement |
| Disconnect after partial fill | Blocked until reconciliation | Query cumulative executions and book missing deltas only |
| Duplicate execution | Existing identity only | Event journal deduplicates; cumulative replay does not debit twice |
| Cancel races with fill | No assumption of cancellation | UNKNOWN outcome requires query; confirmed execution wins |
| UNKNOWN resubmission | Forbidden | Query/reconcile the original client identity |
| Persisted stock state disagrees with authority | Blocked, kill switch enabled | Resolve cash/holdings/orders conflict; reconciliation does not clear kill switch |
| Stale quote / missing source event time | Forbidden | Obtain a fresh, trusted observation |
| Insufficient risk budget / margin | Forbidden | Explicitly change funding or configured limits before reassessment |
| Kill switch | New risk forbidden | Explicit operator action; other account remains unchanged |
| Network order / cancel / configuration mutation | Forbidden by existing safety gate | Offline transports only |

Stocks restored without protocol context remain query-only for existing orders;
recovery does not synthesize exchange observations to resume fills.

## Operational boundaries

A continuous real-time paper session still needs supervised quote/session/rule
refresh, stream lifecycle handling and a durable operator-driven reconciliation
loop. FakeTransport is an in-process exchange fixture, not a persistent exchange
simulator across process termination; the exercise restarts local controllers
while retaining that independent authority.

Real-account execution remains blocked. It would require separately authorized
execution transports, account/permission and product eligibility evidence,
settlement/custody evidence for stocks, exchange-grade reconciliation and
operational controls. This exercise provides none of those approvals.

## Stocks public observation policy

Public Paper rules require a locally supplied `BINANCE_EQUITY_API_KEY` with
MARKET_DATA access. The unsigned GET needs no secret or trading permission.
Rule acquisition makes one request without retries; diagnostics distinguish
missing/invalid local authentication, explicit permission denial, explicit
geographic restriction, other HTTP errors, network failure and an absent target.
An unclassified HTTP 403 is not evidence of a geographic restriction. Rules
remain UNKNOWN on failure. Upstream error bodies are not retained. A verified
response source is separate from executable rule completeness: missing `minQty`
remains UNKNOWN, not zero or `stepSize`. The pinned symbol schema defines a
minimum quantity independently of the increment; its absence does not certify
that no minimum applies.

The initial Stocks phase comes from NVDA's `mp` in the official `price` stream,
not a guessed opening schedule. Calendar transitions subsequently update it.
Only NVDA phase evidence is retained from a market-wide price snapshot; those
prices do not supply executable quotes. Both rules and phase observations have
a conservative 300-second Paper validity window, not an exchange guarantee.
Price frames may omit NVDA. Such a frame supplies no NVDA observation: it neither
authorizes an initially unknown phase nor revokes or renews a prior observation.
An NVDA entry with an absent or unknown `mp` instead revokes phase approval.
Contradictory observations without a reliable temporal ordering revoke phase
approval until a newer consistent Calendar transition or a new connection's
initial observation. Disconnect and restart revoke approval; operator start
alone cannot restore it. Replay and injected transports cannot authenticate
public Paper observations.

The [pinned official price schema](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/websocket_streams/models/price_stream_response_rates_inner.py)
defines `t` as nullable **price time**. It is not phase-effective time. Snapshot
freshness uses the official connection's receipt time; an old price neither
opens a phase nor makes a newly received `mp` snapshot a historical transition.
The [Calendar schema](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/websocket_streams/models/calendar_stream_response.py)
defines `ts` as the time the transition was **detected**. Actual phase-effective
time remains unknown. Local receipt, price time and transition-detection time
are kept separately. See the [official Calendar wire fixture](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/tests/unit/websocket_streams/test_market_streams_api_ws_streams.py)
for the long-form phase names, including `PRE_MARKET` and `MARKET_OPEN`.

A fresh phase and valid market rules authorize only the explicitly selected
synthetic Paper account policy, never real-account eligibility or live orders.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
