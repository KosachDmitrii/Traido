# Scanner cost audit — the state before the universe refactor

Phase 0 of the universe work. Every number here was measured, not estimated;
`scripts/audit_scan_cost.py` counts live HTTP by host by wrapping the transport
underneath every provider, so a call cannot escape the tally by building its own
client — which several of them do.

Measured 2026-08-31, US market open, Alpaca + Finnhub keys configured, FRED key
absent, watchlist timeframes `1d`/`1h`, 400-day lookback.

## Cost of one scanned symbol

```
symbol  status                  seconds   requests by host
AAPL    no_candidate               4.24   alpaca=12, finnhub=1  total=13
MSFT    no_candidate               3.96   alpaca=12, finnhub=1  total=13
NVDA    no_candidate               4.03   alpaca=12, finnhub=1  total=13
XLE     no_candidate               4.03   alpaca=12, finnhub=1  total=13
JNJ     no_candidate               3.48   alpaca=10, finnhub=1  total=11
```

**≈3.95 s and ≈12.6 HTTP requests per symbol**, for a symbol that produces no
candidate — which is the common case, and therefore the case that sets the
budget.

### Where the requests go

| Call | Requests | Why |
|---|---|---|
| `get_bars(D1, 400d)` | 1 | ~280 daily bars fit one page |
| `get_bars(H1, 400d)` | 9–11 | ~2600 hourly bars, paginated at ~207/page |
| `assess_news` → Finnhub `company-news` | 1 | no cache; every symbol, every cycle |
| `assess_market` → FRED ×2 | 0 here, **2 if configured** | no cache; the macro read is identical for all symbols and is re-fetched per symbol |
| earnings calendar | 0–1 | candidates only, 6 h cache |
| broker account | ~0 | `ScanContext.portfolio()` reads once per cycle |
| Anthropic | **0** | no LLM anywhere on the scan path |

### Where the seconds go

Effectively all of it is the hourly bar pagination: 10–12 sequential HTTP round
trips at roughly 0.3 s each. Technical, quant and strategy work is local
arithmetic and does not register.

## Cost of one 60-symbol cycle

| | measured / derived |
|---|---|
| Wall clock | 60 × 3.95 s = 237 s, plus 60 × `SCAN_PACING_SECONDS` (0.4 s) = 24 s → **≈261 s** |
| Configured interval | 300 s |
| Duty cycle | **≈87 %** — the scanner is nearly saturated |
| Alpaca market-data requests | **≈760** |
| Finnhub requests | 60 |
| Alpaca broker requests | 2 |
| Anthropic requests | 0 |

760 requests over 261 s is **≈175/min against Alpaca's 200/min basic limit** —
about 87 % of the rate budget as well as 87 % of the clock. Both ceilings are
reached at the same point, which is why the current number works at all.

## Where the "60" comes from

Not a list of sixty symbols. The full universe is 166 names, derived in
`core/universe.py` from `configs/universe.json` (11 sector groups plus the
`broad` and `sector` ETF sets). `max_symbols_per_cycle: 60` in
`configs/watchlist.json` caps **one cycle**, and `_scan_cursor` rotates the
starting point so the 166 are covered in three cycles.

So the limit is not a list to lengthen. It is a per-cycle work budget, and the
measurements above show it is set at very nearly the largest value the current
architecture can sustain.

## Maximum safe universe under the current architecture

Two independent ceilings, and they agree:

- **Clock:** 300 s ÷ 4.35 s per symbol (including pacing) → **≈69 symbols**
- **Rate limit:** 200 req/min ÷ 12.6 req/symbol = 15.9 symbols/min × 5 min → **≈79 symbols**

**≈60–70 symbols per cycle is the real ceiling**, and the configured 60 sits just
under it. There is no configuration change that reaches 1000; the per-symbol
cost has to change.

Extrapolated to 1000 symbols on the current design:

- 1000 × 3.95 s = **66 minutes** of wall clock per cycle
- 1000 × 12.6 = **12 600 requests**, which at 200/min is **63 minutes** of rate limit

Both are ~13× over a five-minute cadence. This is the reason the refactor is
structural rather than numerical.

## Structural findings

1. **Every symbol pays for deep analysis before anything decides it is
   interesting.** `run_symbol_pipeline` fetches 400 days of hourly bars and a
   week of news for a symbol whose price may disqualify it in one comparison.
   There is no cheap filter in front of the expensive work.

2. **A new market-data port is constructed per symbol.**
   `trading/pipeline.py:85` calls `build_supervisor(settings)` inside the loop,
   and `agents/supervisor/agent.py:278` builds a fresh `AlpacaMarketData` in it.
   The `ScanContext` port is used only for correlations and risk. So the one
   object designed to be shared for the cycle is bypassed by the hottest path.

3. **Nothing is batched.** Alpaca serves multi-symbol snapshots and multi-symbol
   daily bars; the scanner uses neither. This is where three orders of magnitude
   are available: 1000 symbols of snapshot data is ~5 requests, against ~12 600
   for the same names one at a time.

4. **No bar cache.** Daily bars for a symbol are re-fetched every cycle even
   though the previous daily bar has not changed.

5. **The macro read is per symbol.** `assess_market()` calls FRED twice for each
   symbol, uncached, for an answer that is identical across the whole cycle.

6. **Concurrency is one.** The loop is strictly sequential with a fixed 0.4 s
   pause. There is no pool, no per-provider budget and no way to raise
   throughput other than removing the pause.

7. **Scheduling is sleep-after-finish**, not a cadence. A cycle that overruns
   silently pushes the next one later, and nothing reports that it happened.

8. **The funnel loses candidates.** `ScanFunnel` counts what the loop reached;
   symbols never reached because a cap cut the pass short are not represented in
   any terminal bucket.

9. **No instrument identity.** There is no asset class, exchange, currency or
   tradability anywhere in the scan path. `check_instrument_eligibility` exists
   but runs only at execution — a symbol's eligibility to be *analysed* is never
   asked.

## What the refactor has to change, in order of leverage

1. Batch market data (removes ~99 % of the requests)
2. A cheap deterministic filter before deep analysis (removes ~95 % of the seconds)
3. Bounded concurrency with real provider budgets (raises the ceiling that is left)
4. Instrument identity and structural eligibility (makes a large universe safe)
5. Cadence scheduling and complete funnel accounting (makes it observable)
