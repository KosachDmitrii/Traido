# Traido vendors — 2026-09-10

| Function | Provider |
|---|---|
| Execution, account, positions, fills | Alpaca Paper only |
| OHLCV and quotes | Alpaca; IEX development feed |
| News / earnings / sector | Finnhub |
| Macro | FRED |
| LLM | Anthropic Claude |
| Notifications | Telegram |
| Persistence | PostgreSQL and Redis |

Live execution is disabled. No selectable execution backend or workstation
session is required. Mock execution requires explicit local test configuration.

Credentials: `ALPACA_API_KEY`, `ALPACA_API_SECRET`.
Data URL: `https://data.alpaca.markets`; `ALPACA_DATA_FEED=iex`.
Execution URL: `https://paper-api.alpaca.markets` (exact endpoint enforced).

See [Alpaca-only decision and migration](alpaca-only.md).
