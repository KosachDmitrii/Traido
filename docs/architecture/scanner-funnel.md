# The scan funnel

How the desk goes from a vendor's asset feed to a handful of proposals, and why
each stage is where it is. The measured "before" numbers are in
[scanner-cost-audit.md](scanner-cost-audit.md); this describes what replaced it.

## The problem the shape solves

The previous cycle walked a capped list and gave every name the full treatment:
400 days of hourly bars, a week of news, technical, quant and strategy passes.
Measured on the live path that was **3.95 s and 12.6 HTTP requests per symbol**,
which put the ceiling at about **69 names** per five-minute cadence. The
configured cap was 60, sitting just under it. There was no number to raise: at
1,000 names the same cycle would take 66 minutes and issue ~12,600 requests.

So the work is staged by cost. Each stage is cheaper per name than the one after
it, and admits fewer names to it.

| Stage | What it decides | Data | Names |
|---|---|---|---|
| 0 | May we look at this at all? | Reference data, cached for hours | thousands |
| 1 | Is it liquid and alive today? | One batched snapshot read | thousands → ~150 |
| 2 | Is it worth an expensive look? | One batched daily-bar read | ~150 → ~30 |
| 3 | Is there a trade here? | The existing per-symbol pipeline | ~30 → ~20 |
| 4 | May we take it? | The existing deterministic risk engine | ~20 → a few |
| — | Rank → capacity → publish | | → 1–5 cards |

Nothing about *how a trade is judged* changed. Stage 3 is `run_symbol_pipeline`
and Stage 4 is the risk engine; every gate that protects capital still runs, in
the same order, on everything that reaches it. What changed is how few names
reach it, and that the funnel can now say where all the others went.

## Where each piece lives

| Concern | Module |
|---|---|
| Instrument identity, tiers, reason codes | `universe/models.py` |
| Providers (curated, Alpaca, static) | `universe/provider.py` |
| Stage 0 structural eligibility | `universe/eligibility.py` |
| Universe resolution + reference cache | `universe/service.py` |
| Stage 1 cheap market filter | `agents/scanner/prefilter.py` |
| Stage 2 quant pre-ranking | `agents/scanner/prerank.py` |
| The cycle itself | `agents/scanner/cycle.py` |
| Cadence and overrun policy | `agents/scanner/schedule.py` |
| Funnel accounting | `agents/scanner/funnel.py` |
| Concurrency, rate budgets, AI budget | `core/concurrency.py` |
| Cache provenance | `core/freshness.py` |
| Batched vendor reads | `market_data/providers/alpaca.py` |
| Per-cycle shared state | `trading/scan_context.py` |
| Metrics | `core/metrics.py`, exposed at `/metrics` |

Scanner code never names a provider. It asks a `UniverseProvider` and gets
`Instrument`s back, which is what makes the source swappable without the
eligibility policy changing underneath it.

## Failure behaviour

The scanner may only ever *remove* candidates, so the failure to design against
is not letting something bad through — it is discarding something good for a
reason that is really a data problem, or losing a name silently.

| Situation | Behaviour | Funnel bucket |
|---|---|---|
| Universe provider unconfigured | Fall back to the curated CORE list | — |
| Universe provider malformed reply | Raise; cycle reports an error | — |
| Snapshot batch fails | Whole batch recorded as failed, cycle stops | `provider_failed` |
| Daily-bar batch fails | Same, for the Stage 1 survivors | `provider_failed` |
| One malformed bar row in a batch | Row dropped, batch kept | (fewer bars → `quant_rejected`) |
| Symbol absent from the snapshot batch | Rejected, named | `market_filter_rejected` / `NO_SNAPSHOT` |
| Missing or non-positive price, missing volume | Rejected, never defaulted to zero | `market_filter_rejected` |
| Crossed book | No spread reported; rejected as bad data | `market_filter_rejected` |
| Snapshot older than the age limit | Rejected | `data_stale` |
| Fewer than 60 daily bars | Not scored | `quant_rejected` |
| Daily series older than 5 days | Not scored | `quant_rejected` |
| One symbol raises in deep analysis | Recorded; the scan continues | `deep_analysis_failed` |
| AI budget exhausted | Refused in ranked order | `ai_budget_exhausted` |
| Book already holds the symbol | Never analysed | `position_open` |
| Card already open for the symbol | Never analysed | `duplicate_symbol_rejected` |
| Desk queue full at cycle start | Cycle pauses, says so, retries sooner | `capacity_rejected` |

Every instrument that enters is in exactly one terminal bucket when the cycle
ends. `ScanFunnel.reconciles()` asserts it, a test asserts it over a
thousand-name run and over a run where a third of deep analysis fails, and
`traido_scan_funnel_unbalanced_total` counts it in production.

## Determinism

Three separate mechanisms, because one would be a coincidence:

- Stage 1's cut is by traded value then symbol, never by arrival order.
- Stage 2's score is rounded before comparison and tie-broken by symbol, so
  floating-point noise cannot decide a place.
- The final ranking is confidence, then reward-to-risk, then symbol.
  `ConcurrencyManager.map` returns results positionally, so completion order
  never reaches the sort in the first place.

## Cadence

Cycle *n* is due at `origin + n × interval`. Finishing early waits for the slot;
finishing late is reported as `SCAN_OVERRUN` and the schedule skips to the next
*future* slot rather than running back-to-back to catch up. A scanner behind
schedule is already short of provider capacity.

## Measured cost

Against deterministic fakes (`scripts/benchmark_scanner.py`, median of 3):

| Universe | Stage 0 cold | Stage 1 | Stage 2 | Deep analyses | Total |
|---|---|---|---|---|---|
| 100 | 0.2 ms | 0.6 ms | 44 ms | 20 | 47 ms |
| 500 | 0.5 ms | 2.9 ms | 71 ms | 20 | 78 ms |
| 1000 | 1.0 ms | 6.5 ms | 71 ms | 20 | 81 ms |

Live, against Alpaca: 14,276 instruments in one request in 1.5 s; 851 symbols
priced in 1.4 s across 5 HTTP requests, versus roughly 10,700 requests and 56
minutes on the old per-symbol path.

The scanner's own cost is therefore no longer the limit. What remains is Stage 3
— unchanged, still ~4 s per name — which is why `deep_analysis_top_k` is the
setting that decides how long a cycle takes.
