# ALP-E02 online evidence — 2026-11-24

The live checks ran against Alpaca data and Paper Calendar/Clock during the US regular session. No orders were sent by this acceptance runner.

| Capability | Actual observation |
|---|---|
| Historical daily | All 8 symbols × IEX/SIP × raw/split/dividend/spin-off/all returned exactly 252 sessions, 2025-11-21–2026-11-23 |
| Daily quality | No missing scheduled sessions, duplicate timestamps, illegal OHLCV or off-calendar daily labels in the requested window |
| Historical minutes | All 8 symbols on both feeds returned data for 2026-11-20 and 2026-11-23; explicit New York boundaries; no duplicate/illegal rows |
| IEX latest quote/trade/snapshot | HTTP 200; all 8 symbols represented |
| SIP latest quote/trade/snapshot | HTTP 403; historical access does not imply real-time access |
| IEX WebSocket | All 8 symbols observed; actual intentional socket close, reconnect, subscription restoration and REST quote/trade backfill |
| SIP WebSocket | Server error 409, insufficient subscription; no alternate feed selected |
| Delayed SIP WebSocket | Connection/subscription and some market events observed, but REST recovery unsupported; not accepted as recovered |
| Calendar | Alpaca and XNYS schedules agreed over the comparison interval, including open/close instants |
| Clock | Official current market state agreed with the session calendar; approximately 167 ms observed timestamp offset in the diagnostic sample |
| Incremental cache | Two actual AAPL daily ingestions published distinct immutable generations; cross-market provider returned 2 rows |
| Strategy input bundle | 252 × 8 adjusted prior-session matrix, all history masks valid; all 8 current-session opening observations obtained |
| Opening-to-quote causality | 6 quote observations passed; BA/JPM were explicitly invalid because latest quote events preceded receipt of their opening input, despite later HTTP receipts |

IEX latest-quote event age at HTTP receipt ranged from approximately 0.17 to 4.82 seconds in one sample. This measures event age, not pure network latency or a latency guarantee.

Vendor daily volume/close can differ from observed RTH-minute aggregates; see `adjustment-report.md`. Missing minute bars are not filled or automatically called a download failure. The initial minute request used UTC civil-date boundaries and included the previous New York evening for SIP. The request implementation was corrected to explicit New York boundaries, and the minute check was rerun; original responses remain in local evidence rather than being erased or presented as the corrected result.

`portfolio-inputs-online.json` records the subsequent portfolio-input check at 2026-11-24T15:59:45Z: existing IEX cache reused without historical network requests; all eight symbols have 252 valid close/open/volume observations under the explicit Yahoo-formula projection. No opening observation was required. One batch IEX quote response had six valid quotes under a five-second TTL and strict post-decision event/receipt cutoff; BA was stale and pre-decision, while GE was pre-decision. Neither was retried or promoted to a fill. This verifies formula projection and causal inputs, not Yahoo/Alpaca data equivalence.

`coverage.csv`, `data_quality.json`, `sources.json`, `strategy-inputs-online.json`, and `redacted-control-evidence.json` contain compact reviewed evidence. Original payloads, complete ratio rows, initial minute responses and full cache generations remain in ignored local `results/` storage. The public artifacts contain no credentials, account/order identifiers or machine-specific credential paths.

The per-symbol input timestamps demonstrate that an HTTP response received after opening observation does not necessarily contain a newer quote. Invalid observations do not become implicit same-price opening fills.

149 focused Python 3.14 tests passed across market-data REST/stream/input/service, existing cache publication/process concurrency and cross-market calendar contracts. The portfolio finalization then passed 13 targeted input/service tests, including late adjusted receipts, distinct vendor/Yahoo-formula volume, batch post-decision quotes and bounded cache query dates. Earlier subprocess failures were due to missing source import paths in the new test environment; rerunning with `PYTHONPATH=src` passed the process tests.

Original v69 adjustment equivalence, strict PIT/HFQ admission, official primary-exchange auction prices, long-history SNDK/GE security continuity, delayed-SIP gap recovery and BOATS/overnight/OTC account permissions are not established by this run. These do not prevent use of the explicitly evidenced IEX service or permitted historical SIP service. See the adjustment report for the observed SNDK 8-for-1 split boundary.
