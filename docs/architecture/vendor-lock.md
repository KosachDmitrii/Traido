# Vendor Lock — Professional Choices (all stages)

Owner decision: Traido engineering lock, 2026-08-30.  
No further vendor bikeshedding unless a hard blocker appears.

## Locked stack

| Concern | V1 → Stage 6 (paper) | Stage 7+ (scale / live) | Rationale |
|---------|----------------------|-------------------------|-----------|
| **Execution** | **IBKR Paper** (Alpaca Paper until the adapter lands) | **IBKR Live** | Revised 2026-08-30 — see decision record below |
| **OHLCV / quotes** | **Alpaca Market Data** | optional **Massive** for wide scan | One vendor for paper loop; Massive later if universe scan needs depth/history |
| **News / earnings / sector** | **Finnhub** | Finnhub paid / multi-source | News, earnings calendar, and `profile2` sector for names outside `universe.json` |
| **Macro** | **FRED** | FRED | Official series, free, regime inputs |
| **Fundamentals** | Stage 5+ stub → **SEC EDGAR** | EDGAR + paid if needed | No paid fundamentals required for V1 entries |
| **LLM** | **Anthropic Claude** | Claude (multi-model optional) | Structured JSON assessments only |
| **Confirm notify** | **Telegram Bot** | Telegram + PWA push | Mobile-first for personal desk; API confirm remains source of truth |
| **DB / queue** | PostgreSQL 16 + Redis 7 | same | Already locked |

## Operating principle

```
Alpaca Market Data  →  Quant / Agents  →  Risk Engine  →  Confirm (Telegram + API)
                                                      →  Alpaca Paper execution
```

Live capital (post Stage 7 gate): same pipeline, `BrokerPort` → IBKR Live.  
Market-data provider may stay Alpaca or upgrade to Massive without rewriting agents.

## Env keys (names)

| Env | Provider |
|-----|----------|
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | Paper trading + market data |
| `ALPACA_DATA_BASE_URL` | default `https://data.alpaca.markets` |
| `ALPACA_BROKER_BASE_URL` | default `https://paper-api.alpaca.markets` |
| `FINNHUB_API_KEY` | News · earnings calendar · sector (`/stock/profile2`) |
| `FRED_API_KEY` | Macro |
| `ANTHROPIC_API_KEY` | Agents |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Confirm notifications |
| `TRAIDO_IBKR_ENV` | `paper` (default) or `live`. Unset means paper |
| `TRAIDO_IBKR_HOST` / `TRAIDO_IBKR_PORT` / `TRAIDO_IBKR_CLIENT_ID` | IB Gateway or TWS session |
| `TRAIDO_IBKR_ACCOUNT` | IB account id. Not a secret; IB authenticates through the Gateway session |

Legacy aliases `BROKER_API_KEY` / `MARKET_DATA_API_KEY` map to Alpaca in settings for compatibility.

## Decision record — execution broker (2026-08-30)

**Superseded:** "Alpaca Paper for V1, IBKR at Stage 7+."
**Now:** IBKR is the execution broker. IBKR Paper for testing, IBKR Live for
production.

Rationale: paper and live must share one architecture (`ARCHITECTURE.md`
invariant 6). Proving the desk against Alpaca's REST semantics and then
switching venues at the live gate would validate the wrong execution model —
TWS/Gateway session handling, pacing limits, partial-fill behaviour and order
state transitions differ enough that the migration itself becomes the risk,
precisely at the moment real capital arrives.

Cost accepted: Gateway/TWS operational overhead during development.

**Migration constraint:** the Alpaca adapter stays until IBKR passes the same
lifecycle tests. Removing a proven execution path before its replacement is
proven would violate capital safety.

Unchanged: market data remains Alpaca. The broker decision does not move it.
Market-data vendor and execution broker are independent choices, and neither is
allowed to be inferred from the other.

### IBKR adapter status (Stage 7.1)

Precise, because "the IBKR adapter works" can mean several different things:

| Claim | Status |
|-------|--------|
| Adapter implemented against `BrokerPort` | yes — `broker/ibkr/adapter.py` |
| IB status vocabulary normalized to domain states | yes — `IB_STATUS_MAP`, plus quantity-based partial detection |
| Instrument identity resolved to a `conId` before any order | yes — `broker/ibkr/instruments.py` |
| Paper/Live separated by configuration and validated | yes — `broker/ibkr/config.py` |
| Real transport implemented against a documented client | yes — `broker/ibkr/live_transport.py` (`ib_async`) |
| Passes the shared broker lifecycle contract suite, entries and exits | yes — against `FakeIBKRTransport` |
| Verified against a real IBKR Paper account | **no** |
| Verified against IBKR Live | **no** |

There is no IB Gateway session in this environment, so no real connectivity has
been exercised. `broker/ibkr/transport.py` ships `UnconfiguredIBKRTransport`,
which refuses every call with an explanatory error rather than behaving like a
connected broker. `TRAIDO_BROKER=ibkr` selects the adapter; Alpaca remains the
default and the only path that has traded.

The contract suite in `tests/contract/` runs the same behavioural tests against
both adapters. Alpaca must keep passing it for as long as it is the live path;
IBKR must pass it, and then pass real IBKR Paper end-to-end, before Alpaca can
be retired.

#### Client library: `ib_async`, not `ib_insync`

`ib_insync` has been unmaintained since its author's death in 2024. `ib_async`
is the direct community fork with the same API surface, so the migration cost
is a rename and the maintenance risk is materially lower. It is an **optional**
dependency (`pip install 'traido[ibkr]'`) and `broker/ibkr/live_transport.py`
imports it lazily — nothing pulls it in unless a deployment explicitly asks for
a live IB session. It is deliberately not re-exported from `broker.ibkr`.

#### Order identity correlation

IB gives an order three identifiers and only one of them is durable:

| Field | Scope | Used for |
|-------|-------|----------|
| `orderId` | one `clientId` session; **reused after restart** | never trusted alone |
| `permId` | account-wide and permanent | persisted as `OrderIntent.broker_perm_id` |
| `orderRef` | free text we own | carries Traido's `client_order_id`; the primary recovery key |

Recovery works because the `client_order_id` is persisted on the intent
*before* transmission and rides to IB in `orderRef`. After a reconnect or a
process restart, `orders_by_ref` finds the order without any in-memory state.
`_trade_to_dict` reports `permId` as the order id whenever IB has assigned one,
so a durable handle is what reaches the domain.

#### Connectivity safety

`IBKRLiveTransport` exposes `connection_state()` over
`BrokerConnectionState`: `DISCONNECTED`, `CONNECTING`, `READY`, `DEGRADED`,
`RECONNECTING`. Only `READY` permits new entries or discretionary exits.
Reconciliation continues in every state, and existing protective orders are left
in place rather than cancelled — the way out of an ambiguous state is to read
more, not to trade more. Emergency exits are not gated on connectivity, but they
go through the same durable intent, so reconnect ambiguity cannot produce a
duplicate flatten.

#### Protective stops are not a safety guarantee

Leaving a stop in place is not the same as knowing it works, and Traido does not
treat it as such. Two limits are worth stating plainly, because a safety
invariant built on either would be built on sand:

1. **Existence must be re-read, not remembered.** A stop can be cancelled at the
   venue, orphaned by an account change, or left the wrong size by a partial
   exit. `reconcile_protective_orders` therefore reads open orders back every
   pass and repairs what it finds. When that read fails, every open position is
   audited as `ProtectionUnverified` at `critical` and named individually in the
   report — an unreadable broker is an unknown protection state, not a clean one.
2. **Triggering belongs to the venue.** IB serves some stop and stop-limit
   orders natively and *simulates* others, holding them in its own systems and
   firing them on a trigger method that varies by product, venue and session. So
   a confirmed, correctly-sized resting stop still does not prove the position
   will actually be exited. This is precisely why the emergency-close path
   exists and why a resting stop never closes out an incident on its own.

**Open item before Alpaca can be retired:** which of Traido's protective orders
IB serves natively and which it simulates has not been established, because that
requires a real IB session. Until it is, the conservative reading applies —
assume simulated, and rely on reconciliation plus emergency close rather than on
the stop.

Ports are fixed per environment and a mismatch is fatal at construction:
paper is 7497 (TWS) / 4002 (Gateway), live is 7496 / 4001. An unset
`TRAIDO_IBKR_ENV` resolves to PAPER — the safe value is the one you get by
forgetting to configure anything.

### Live quotes

The liquidity gate's spread check requires live top of book. `QuotePort` is
separate from `MarketDataPort` precisely so that a bars-only provider cannot
satisfy a spread check merely by being a market-data provider.
`AlpacaMarketData.get_quote` implements it against the existing vendor — this
is more of the same feed, not a new one. Without a fresh quote the gate rejects
with `LIVE_QUOTE_REQUIRED` or `QUOTE_STALE`.

## Why not Massive from day one

Cost and complexity. Interfaces already allow swap. Buy Massive when paper strategy needs broader realtime scan than Alpaca provides.
