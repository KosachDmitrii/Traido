# scalper

Standalone research agent for short-horizon (scalping) strategies on US equities.
It shares no code, config or credentials with Traido and can be moved into its own repository as is.

## Status

| Piece | State |
|---|---|
| `collector` (live trades + quotes → parquet) | built |
| `features`, `signals`, `risk`, `backtest`, `execution` | empty packages, in this order |

No order placement exists yet, by design. Order of work: record data → backtest with realistic costs → paper run → only then anything else.

## Design rules

- Signals, risk limits and order logic are deterministic code with thresholds in config. An LLM, if used at all, only classifies market regime or reviews trades after the fact. It never decides an entry or an exit.
- A missing or stale quote is a refusal, not a pass.
- Any calendar day is the exchange's (`America/New_York`), never UTC's.
- Credentials come from environment variables only and are never logged.

## Setup

```bash
cd scalper
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env     # fill in a SEPARATE Alpaca paper account's keys
set -a; source .env; set +a
```

## Use

```bash
scalper check      # verifies keys and that the configured feed (sip) is allowed
scalper collect    # records config/collector.toml symbols until Ctrl+C
pytest
```

Output: `data/<trades|quotes>/<SYMBOL>/<YYYY-MM-DD>/part-*.parquet` (zstd). Each row carries the exchange timestamp `ts` and the local `recv_ts`, so feed latency can be measured later.

## Notes

- Run `collect` on a machine that stays on during market hours (a VPS, not a laptop that sleeps).
- Quotes are high volume. Five liquid names can reach several GB per week; watch disk space.
- Alpaca allows a limited number of simultaneous WebSocket connections per account. Do not run two collectors on one key.
- `rth_only = true` drops pre/post-market ticks. Change it in `config/collector.toml`.
