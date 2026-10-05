# Company-level sector directory fallback — 2026-10-05

Owner request: resolve ambiguous sector metadata across all discovered equities,
not by adding a permissive mapping for FLEX's `Electrical Equipment` industry.

Decision: retain curated classifications, explicit Alpaca ETF identity and exact
Finnhub industry mappings. If Finnhub is missing, unavailable or unclassified,
read Nasdaq's public stock screener company-level **sector** field. Its industry
field is evidence only; it never selects a sector. No new subscription or key.
Nasdaq categories are not claimed to be GICS. Explicit sector labels are mapped
to the existing eleven internal buckets and their configured benchmark ETFs.

Source: https://www.nasdaq.com/market-activity/stocks/screener
Endpoint: https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true
Live validation on 2026-10-05 returned 7001 rows, 6266 with validated sectors
across all eleven groups, including FLEX with explicit
`Technology` sector and `Electrical Products` industry. FLEX is not hardcoded.

One bulk read is shared across symbols. Metadata-only rows are persisted in the
existing sector evidence store under a reserved directory key, excluded from
per-symbol diagnostics/hydration. No broker, quote or trading state is stored
there. Single-flight refresh, seven-day evidence TTL and two-minute failure
cooldown prevent per-symbol request storms. Vendor reads use bounded retries
and a 30-second total deadline. Valid persisted evidence restores after restart.
The source timestamp is preserved when a directory entry becomes symbol evidence:
looking up an old entry never gives it a new seven-day validity window.

Exact symbol matching, explicit recognized sector, valid schema and nontruncated
catalogue are mandatory. Conflicting duplicate companies and unknown sectors are
excluded. Expired, future, timezone-naive, mismatched or unsupported-version
stored evidence is rejected. A failed refresh cannot authorize expired data.
Old failed Finnhub cache entries are invalidated by resolver v5; reproducible
v3/v4 successful Finnhub evidence remains usable for its original TTL.

An unavailable directory or genuinely absent company remains blocked if Finnhub
cannot classify it. This improves coverage; it does not guarantee universal vendor
availability. Admission, benchmark freshness, concentration limits and all broker
controls are unchanged. OHLCV and quotes remain Alpaca SIP; execution stays Paper.
