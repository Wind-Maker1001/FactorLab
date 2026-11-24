# Alpaca adjustment reconciliation — 2026-11-24

## Observed window and source basis

All eight symbols returned 252 daily observations from **2025-11-21 through 2026-11-23**, independently for IEX and SIP and for raw, split, dividend, spin-off and all. Each series has zero duplicate timestamps, illegal OHLCV rows or missing expected sessions in this window. IEX volume is single-exchange volume; SIP is consolidated. No feed substitution was used.

Raw OHLCV is retained separately from adjusted prices and volume. Split adjusts price and volume, dividend adjusts price, spin-off adjusts prices for separations, and all includes all three categories. The per-symbol observed ratio ranges are recorded in `data_quality.json`; supplier rounding means price ratios need not equal an exact rational factor at every price.

## SNDK split evidence

The supplier's corporate-action endpoint returned a forward split for SNDK, CUSIP `80004C200`, old_rate=1, new_rate=8, ex_date/process_date **2026-11-20**, payable_date **2026-11-19**, record_date **2026-11-09**. Its action ID is `c87ee742-a99f-4454-9955-918467e2a24e`.

The price/volume boundary agrees with those terms: on 2025-11-21 the SIP raw close was 200.27 and split close 25.03; raw volume 20,060,990 became 160,487,920, exactly 8×. On 2026-11-20 the raw and split closes were both 79.24 and volume was unchanged. The small departure of close ratio from exactly 1/8 reflects the returned price rounding. Those raw pre-split prices must not be replaced by adjusted prices when computing historical order shares.

The same response includes a TQQQ 2-for-1 split effective 2025-11-20, immediately **before** the 252-session window. TQQQ raw/split ratios inside this window were consequently 1. GE raw/split/spin-off ratios inside this window were also 1; that is not evidence that GE had no earlier reorganization.

This finite-window check does not certify that older SNDK ticker history represents the current security. Long-history SNDK listing/separation identity and GE's historical separations require dated security-master evidence before extending the continuity claim. Queries retain an explicit symbol-mapping as-of date rather than blindly joining same-ticker entities.

## Daily and RTH scope

For AAPL on 2026-11-20, the 390 observed SIP RTH minute bars summed to 55,128,106 shares, while the supplier daily volume exceeded that sum by 25,741,470. Daily close also differed by -0.19. IEX daily volume exceeded its 390-minute RTH sum by 1,409 shares while its OHLC matched. These observations show that vendor daily aggregates are not interchangeable with observed RTH minute aggregates. Extended-hours trades, eligible-trade rules, closing-auction treatment and supplier revisions must be distinguished; the difference is not attributed to one cause without further evidence. The integration retains vendor daily scope and separately filters minutes by scheduled RTH boundaries.

## Research equivalence

Alpaca's corporate-action queries filter/sort by **process_date**, not ex-date, announcement time or historical publication time. Returned action terms can corroborate an observed adjustment, but present-day retrieval does not establish historical availability. Empty responses do not establish verified no-event coverage.

**PIT_UNVERIFIED / HFQ_UNVERIFIED.** Separate immutable response vintages preserve acquisition evidence, not a certification of historical knowledge. **Original v69 adjustment equivalence remains UNCONFIRMED** until its original data source and adjustment convention are available. The usable market-data service does not depend on resolving that separate research question.
