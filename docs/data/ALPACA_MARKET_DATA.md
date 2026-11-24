# Alpaca equity market data

Install the `alpaca-data` optional dependencies with the repository's Python 3.14 environment. Supply `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` to the process, or explicitly select a Windows DPAPI-encrypted SecureString XML with `Credentials.from_dpapi(path)` / `--credentials-file path`. The encrypted file is user-bound and must live outside the repository; decrypting it requires the same Windows user context. Credentials are never part of cache identities or evidence. All HTTP requests in this integration are GETs to an enumerated data/calendar/clock endpoint. It has no order API.

## Historical and online consumers

```python
from pathlib import Path
import pandas as pd
from factorlab.integrations.alpaca.market_data.client import Credentials, MarketDataClient
from factorlab.integrations.alpaca.market_data.cache import MarketCache
from factorlab.integrations.alpaca.market_data.inputs import Instrument
from factorlab.integrations.alpaca.market_data.service import MarketDataService

client = MarketDataClient(Credentials.from_env(), feed="iex")
service = MarketDataService.connect(client, MarketCache(Path("results/alpaca-cache")),
                                    start="2025-01-01", end="2026-12-31")
# Bind this identifier to the verified, dated security master, not just a ticker.
instrument = Instrument("verified-alpaca-asset-id", "AAPL", pd.Timestamp("2026-11-24T12:00:00-05:00"))
result = service.query(instrument, "2025-11-20", "2026-11-23", adjustment="raw")
```

`query` returns the existing `QueryResult` / `SourceMetadata` envelope without routing US tickers through mainland-specific DataHub normalization. The cache also exposes the existing cross-market `ParquetDataProvider` interface. Identity `asof` is converted to its New York civil date for Alpaca symbol mapping; it is not a knowledge cutoff. A ticker match alone is not proof of security continuity.

Raw queries retain `raw_open/high/low/close/volume` as well as the ordinary OHLCV aliases. Adjusted queries expose `adjusted_*` independently; they never replace raw execution prices. Source, feed, adjustment, mapping as-of, calendar version, original vendor timestamps, receipt timestamps, request coverage and immutable generation are retained. A successful empty request covers an acquisition interval, not an observed trading session.

`prepare_history(instruments, start=..., end=..., adjustment="all", adjustment_basis="yahoo_ratio")` prepares prior-session matrices before the execution window. With no `now`, availability is evaluated after acquisition; an explicit aware `now` applies that cutoff, masking later receipts. `inputs(..., prepared=prepared, use_opening=False)` exposes `adjusted_close`, `adjusted_open`, `adjusted_volume` and their `*_validity` masks directly; `history`/`validity` remain close aliases. Both raw and adjusted completion/receipt metadata govern availability. The matrices exclude the current New York session even after its close.

Original v69 forms weekly intent from the previous complete session, not an opening observation. The explicit `yahoo_ratio` projection uses `adjust_ratio=adjusted_close/raw_close`, `adjusted_open=raw_open*adjust_ratio`, and `adjusted_volume=raw_volume/adjust_ratio`. `vendor_adjusted_volume` remains separate in `prepared.bars`; `adjustment_basis="vendor"` retains supplier OHLCV. This implements the original Yahoo formulas without claiming that Alpaca factors or volume equal Yahoo's data. Raw prices remain available for actual-share conversion; the market-data service does not generate orders.

Set `history_client=` in the constructor to use explicitly separate historical and realtime feeds, such as historical SIP and realtime IEX. Sources remain attached to each side of the join. There is no subscription-based fallback or paid-SIP execution gate.

`inputs` completes historical/opening inputs and Clock acquisition before one batch quote request. Its `decision_at` is the portfolio-input cutoff. After strategy calculation and account preparation, execution consumers call `refresh_quotes(instruments, decision_at=..., decision_window=..., max_age=pd.Timedelta(seconds=5))` again. All quotes use one TTL observation instant; event and receipt must strictly follow the decision cutoff. Consumers still recheck TTL immediately before submission. Missing quotes remain `None`; invalid quotes retain reasons. `use_opening=True` is optional and groups opening acquisition by identity-mapping date before requesting any quotes; missing openings invalidate those quotes.

A first observed RTH trade/minute open is not necessarily the official primary-exchange opening auction. The opening helper describes that narrower observation. Quotes must have both event and receipt times strictly after the opening observation's receipt. A stale latest quote therefore does not become executable merely because its HTTP response arrived later.

## Feeds, sessions and revisions

Select a feed explicitly. IEX prices and volume describe one exchange, not consolidated US activity. SIP historical access, SIP live access and delayed SIP streaming are distinct entitlements. Permission failures do not trigger feed replacement. BOATS and derived overnight data are separate sources and are not RTH substitutes.

Calendar responses become the existing `SessionCalendar`; session labels remain civil dates while opens/closes are UTC instants. Actual schedules govern holidays, DST and early closes. `first_session(..., period="week")` uses actual sessions and ISO week identity, including cross-year weeks. Clock disagreements are diagnostic results, not silently corrected schedules.

Vendor daily OHLCV is retained as vendor scope. To study RTH-only prices/volume, filter minute observations by the day's actual open/close and aggregate separately; do not label an unverified daily aggregate RTH-only. Missing minute bars can reflect no eligible trades, halts or data gaps. They are not synthesized.

Raw incremental refresh refetches a configurable overlap. Adjusted refresh covers the complete previously cached window because a new action can revise prices long before that overlap. Feed, timeframe, identity, adjustment and mapping namespaces are isolated. New immutable data generations are published under the shared OS publication lock, with the active manifest switched last. Research `TaskCache` and paper account storage are not used as mutable quote databases.

`AlpacaStream.events()` authenticates, confirms subscriptions, handles timeout/reconnect and restores subscriptions. A reconnect requires an awaited `recover_gap` callback returning exactly `True`; otherwise recovery fails explicitly. The callback owns REST backfill and its observation evidence. Reconnection does not certify exchange-sequence completeness. Quote/trade corrections, cancellations and updated bars are distinct events. Bounded deduplication preserves changed payloads; old replays beyond its capacity can reappear.

## Online acceptance

```sh
PYTHONPATH=src python -m factorlab.integrations.alpaca.market_data.acceptance \
  --symbols AAPL SNDK BA C JPM GE TQQQ QQQ --feed iex \
  --feeds iex sip --stream-feeds iex sip delayed_sip \
  --sessions 252 --stream-seconds 60 --output results/alp-e02-online
```

The session target applies to the completed daily-history request, not as a strategy deployment gate. Minute evidence covers the last two completed sessions. The runner requests raw/split/dividend/spin-off/all daily variants, latest quotes/trades, snapshots, calendar/clock, corporate actions and streaming. Streaming intentionally closes the first live socket after an observed event, reconnects and attempts REST recovery. If a symbol produces no event during the bounded window, it remains explicitly unobserved.

The output includes `coverage.csv`, `data_quality.json`, `sources.json`, `adjustment-report.md` and local raw payloads. Keep the complete output in ignored `results/`; only reviewed, compact, redacted evidence belongs under `docs/validation/`. Do not publish vendor payloads or a full market database by default.

## Adjustment evidence

- `raw`: no supplier adjustment.
- `split`: forward/reverse split price and volume adjustments.
- `dividend`: cash-dividend price adjustment.
- `spin-off`: spin-off price adjustment.
- `all`: all three categories, not merely split plus dividend.

Company-action requests filter/sort by **process_date**, not effective date. Announcement, effective, vendor availability and local acquisition times have different meanings. Unknown historical availability remains unknown. Empty responses do not establish `VERIFIED_NO_EVENT` in `EvidenceActionLedger`; the ingestion layer deliberately does not promote them into verified action coverage.

TODO(ALP-E02): establish dated security-master and corporate-action evidence for SNDK's post-separation listing and GE's reorganizations before claiming long-history identity continuity. Retain vendor `asof` and do not splice old same-ticker entities.

TODO(ALP-E02): reconcile Alpaca adjustment factors against dated Yahoo provider observations; the original v69 formulas are implemented explicitly, but cross-provider data equivalence remains unconfirmed. Current vendor-adjusted history is **PIT_UNVERIFIED / HFQ_UNVERIFIED**.

## Official interface references

- [Historical bars](https://docs.alpaca.markets/us/reference/stockbars): symbols-first pagination, total page limit, `adjustment`, `asof`, explicit `feed`.
- [Historical stock feeds](https://docs.alpaca.markets/us/docs/historical-stock-data-1).
- [Stock WebSocket protocol](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data).
- [US Calendar](https://docs.alpaca.markets/us/reference/legacycalendar) and [Clock](https://docs.alpaca.markets/us/reference/legacyclock).
- [Corporate actions](https://docs.alpaca.markets/us/reference/corporateactions-1).
