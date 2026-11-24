# V69-F01 — partial validation, execution not verified

Base: origin/main 5e9f29a. Official endpoints: https://fapi.binance.com and https://demo-fapi.binance.com. Raw exchangeInfo snapshots are retained separately; their serverTime is not asserted to be acquisition time. QQQ market acquisition time is in qqq_market_evidence.json.

| Stock | Mainnet EQUITY/TradFi | Demo EQUITY/TradFi | Classification |
|---|---|---|---|
| AAPL | AAPLUSDT | absent | MAINNET_ONLY |
| SNDK | SNDKUSDT | absent | MAINNET_ONLY |
| BA | absent | absent | NOT_LISTED |
| C | absent | absent | NOT_LISTED |
| JPM | JPMUSDT | absent | MAINNET_ONLY |
| GE | absent | absent | NOT_LISTED |
| TQQQ | TQQQUSDT | absent | MAINNET_ONLY |
| QQQ | QQQUSDT | QQQUSDT | DEMO_LISTED_NOT_EXECUTED |

CUSDT is COIN, not evidence of Citigroup support. Mapping is restricted to EQUITY/TradFi metadata, but issuer-level identity remains unverified. Listing does not establish tradability.

## Evidence and limitations

instrument_mapping.json includes the complete matching exchange filters, precision, settlement assets, contract types and statuses. qqq_market_evidence.json includes real book ticker, depth, 100 hourly klines, premium index and funding history responses (or explicit endpoint errors). demo_equity_tradfi.json includes every EQUITY/TradFi row in the captured Demo snapshot. These rows are candidates, not an independently verified list of genuine stock/ETF identities: notably FAKEKR000660USDT and FAHOODUSDT require separate identity review.

No order was submitted. Minimum executable notional requires current filters, quote and account leverage checks together; no minimum executable amount is claimed here. Actual fills, commissions, equity/position reconciliation, leverage limits and restart recovery remain unverified.

The existing execution implementation rejects non-NVDA rules in execution.observe_demo_rules. DemoSession and strategy_runtime also bind NVDAUSDT. Extending this requires a coherent parameterization across admission, network plans, recovery and shared-account ownership; changing a constant or bypassing the gateway would not constitute reuse of the verified execution contract. Azure order ownership coordination has not been established. No existing campaign, position, credential or cloud service was changed. This is an engineering capability gap, not a request for manual trading approval.

## Reproduce

Run `python tools/validate_tradfi_demo.py` from the checkout. It retains the captured exchangeInfo snapshots and refreshes QQQ public market evidence. It does not submit orders or claim online execution acceptance. Full V69-F01 acceptance remains incomplete.
