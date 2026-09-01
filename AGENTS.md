# Cursor / agent notes for this repo

## Capital safety
- Never implement live order placement in V1.
- Never let LLM output call broker or SQL directly. LLMs cannot bypass the risk
  engine, the liquidity gate, the RTH gate, or the order state machine — every
  one of them is deterministic code with its thresholds in config, not prompts.
- Risk lives in `risk/` as deterministic code.
- Prefer rejecting a trade over ambiguous automation.

## Execution state (Stage 7 / 7.1)
- A durable `OrderIntent` is written before anything reaches a broker — entries,
 discretionary exits, and emergency closes alike. One `idempotency_key`, at most
 one broker order. Purpose is recorded (`IntentPurpose`), never inferred from
 BUY/SELL.
- `UNKNOWN` means broker truth is unresolved. It blocks new entries, conflicting
 orders, and autopilot for that symbol until reconciliation clears it. Never
 downgrade it to `CANCELED`, `FAILED`, or "awaiting" to make a flow proceed.
- Any actual filled quantity is protected for exactly that quantity, or
 emergency-closed. A partial fill is a position, not a failure.
- A partial *exit* reduces the local position by the filled quantity only, and
 the protective stop is resized to the remainder. The ledger never closes a
 position twice.
- One open position per symbol, enforced three times over: the entry is refused
 before transmission, the ledger refuses to record a second row, and a partial
 unique index refuses it in the database. The first two hold within a process;
 only the third holds across two. A book with two rows for one symbol can never
 agree with a broker that reports a single net position.
- A missing or stale live quote is not a passed spread check. The liquidity gate
 fails closed; modeled spreads are labelled and cannot satisfy a live gate. Nor
 is a missing market-data port: it is `MARKET_DATA_NOT_CONFIGURED`, not a skip.
 Build the execution service through `api.deps.build_execution_service` — a
 route that constructs its own can leave a gate unarmed, which is exactly how
 the liquidity gate came to be written, tested, documented and never run.
- An earnings calendar that could not be read is not a clear calendar. A stop
 does not survive a print, so the risk engine refuses the entry
 (`EARNINGS_CALENDAR_NOT_CONFIGURED` / `_UNAVAILABLE` / `EARNINGS_UNVERIFIED`)
 rather than treating "no dates" as "no print". `require_earnings_check=false`
 is the only way past it, and it is recorded on every decision taken under it.
- A news feed that could not be read is not a clear news feed. The strategy
 vetoes on negative sentiment, so headlines are a gate, and a missing key or a
 vendor outage means that veto cannot fire — the risk engine refuses
 (`NEWS_NOT_CONFIGURED` / `NEWS_UNAVAILABLE` / `NEWS_UNVERIFIED`) rather than
 reading a neutral 50 as good news. `require_news_check=false` is the only way
 past. A vendor error is returned as a status, never raised: raising took the
 whole symbol's pipeline down and the scanner filed that as `no_candidate`,
 which is the one thing "we could not look" must not be confused with.
- Because those reads fail closed, a dropped request is a refused trade. Vendor
 reads retry through `core.vendor_http` — 5xx, 429 and transport errors, never a
 401, which answers the same way twice. A failure is never cached under the TTL
 chosen for a success: an earnings date holds for hours, "Finnhub did not
 answer" holds for seconds, and one shared TTL took a symbol out of the universe
 until evening over a 503 that lasted a minute. Name a vendor failure by status
 code, never by the vendor's message, which carries the request URL — `HTTP 401`
 and `HTTP 503` call for opposite responses and `HTTPStatusError` distinguishes
 neither.
- No credential reaches a log, the activity board, or the audit trail. Vendor
 keys travel in headers, not query strings, so they are not in a URL to be
 carried into an exception message; `core.redaction` scrubs both by known value
 and by shape inside `BOARD.log`, `BOARD.set_agent` and both audit sinks. Scrub
 at the sink, never at the call site — the leak this replaced arrived through a
 call site nobody had audited.
- Any calendar day the system reasons about is the exchange's, via
 `core.clock.market_date` — never `datetime.now(UTC).date()`, which runs a day
 ahead from 20:00 ET and can file tonight's print as already reported.
- Approval re-derives the risk context; it never re-runs the engine against the
 portfolio alone. A card can wait an hour, and the re-check must be at least as
 strong as the one that drew it — never weaker. A context that cannot be built
 is a rejection.
- New entries and discretionary exits require a `READY` broker link. Emergency
 exits and reconciliation are not gated, because the way out of ambiguity is to
 read more, not to trade less safely.
- A protective order is external state, not something we own. Reconciliation
 re-reads it every pass; an unreadable broker means `ProtectionUnverified`, not
 "protected". Never assume a stop is broker-resident or that it will fire — IB
 simulates some stops, so triggering is the venue's behaviour. Emergency close
 is the backstop, and a resting stop never closes an incident on its own.
- Protective placement carries a durable intent too, keyed
 `protection:{position_id}:{generation}`. A lost reply is looked up by its
 `client_order_id` and a live order adopted — never answered with a second stop,
 and never with a flatten while the first one may still be resting.
- Protection is sized from the smaller of the book and the broker's reported
 position. Sizing from the ledger alone puts a stop above shares the venue does
 not hold, which sells short on the way out.
- Resting protection is swept every pass: a SELL beyond what the venue holds is
 cancelled. Absence of a recorded stop is not absence of a stop.
- New entries are RTH-only. Protective exits and reconciliation are not.
- The kill switch refuses new exposure, never the defence of exposure already
 taken. Protective stops, emergency closes, operator sells and reconciliation
 all continue while halted — the switch is pressed when something has gone
 wrong, so disarming the desk with it would invert its purpose. The broker
 adapter tells them apart by `OrderRequest.purpose`, copied from the intent and
 defaulting to `ENTRY` so an unlabelled order is refused.
- Reconciliation is a control loop, not a page render. It runs on a timer, one
 pass at a time process-wide; concurrent callers join the pass in flight rather
 than starting a second one. Two passes over one unprotected position is how two
 stops get placed.
- New exposure is refused when broker truth is stale or has never been read
 (`RECONCILIATION_STALE` / `RECONCILIATION_NEVER_RAN`). Exits, emergency closes
 and reconciliation are never gated on it.
- Every fact a decision consumes has an age limit — the quote already did, the
 daily bars behind ADV and slippage now do. A feed that stopped a week ago
 returns a pass, not an error.
- Only plain US-listed equities in USD. Sizing, stops and slippage assume a
 listed share; an OTC name clears the liquidity gate on printed volume and has
 no book to exit into.
- Several guarantees here hold per process: single-flight reconciliation, the
 claim locks, the file-backed kill switch. The API therefore refuses to boot
 multi-worker (`core.deployment.assert_single_worker`). Do not add a guarantee
 that quietly needs one worker without adding it to that refusal.
- Failure behaviour is tabulated in `docs/architecture/execution-failure-matrix.md`.
 Change the table in the same commit as the behaviour.

## Architecture (frozen)
- `ARCHITECTURE.md` is the v1.0 conceptual freeze — read it before proposing structure.
- Prime directive: AI thinks · Quant calculates · Strategy proposes · Risk decides ·
  Human or Autopilot authorizes · Broker executes · Reconciliation verifies.
- Open decisions are listed there; do not silently resolve one.

## Stage discipline
- Stage 0 = architecture + contracts only (done when reviewed).
- Implement the next stage only after explicit user approval.
- Do not build the desk UI before Stage 6 (Vite + React in `frontend/`).

## Design (locked)
- Soft warm UI from Cabin / MedSync references — **not** a dark neon terminal
- Tokens: `docs/design/tokens.css` (`#FFCF88` `#B5A18B` `#E4E0E0` `#201F1E`)
- Spec: `docs/design/DESIGN.md`
- References: `docs/design/references/`
- Stage 6 must follow these tokens; do not invent a second palette

## Vendors (locked)
- Execution: IBKR (Paper → Live) · Alpaca adapter stays until IBKR is proven
- OHLCV: Alpaca
- News: Finnhub · Macro: FRED · Notify: Telegram · LLM: Claude
- Details: `docs/architecture/vendor-lock.md`
