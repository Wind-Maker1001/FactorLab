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

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
