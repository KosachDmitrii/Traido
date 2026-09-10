# Alpaca-only decision — 2026-09-10

Authorized by the owner: Alpaca supplies market data and is the sole execution
provider. This supersedes all earlier execution-vendor decisions.
Status remains PAPER_TESTING_ONLY. Manual confirmation, admission, risk limits,
order intents, protective exits and reconciliation remain required.

## Data and execution contract

Development uses `ALPACA_DATA_FEED=iex`. IEX describes one exchange. Alpaca Paper
simulates fills against NBBO, so using one provider does not guarantee that a
displayed quote equals the fill. The interface discloses this distinction.
Keep plan entry, order limit and broker filled average price separate. Never
raise the admitted limit or substitute a market entry to force a fill.

Sources: [Alpaca Paper rules](https://docs.alpaca.markets/us/docs/paper-trading),
[account endpoint](https://docs.alpaca.markets/us/reference/getaccount-1).

## Implementation

The removed adapter, native transport, dependency extra, broker selector,
Gateway deployment scripts, proxy and tunnel are no longer part of the runtime.
The legacy selection PUT returns 410. GET reports Alpaca, Paper and actual feed.
An unsupported deployment selector blocks startup/construction. Old file/Redis
backend preferences no longer control execution. Only the exact HTTPS Paper
endpoint is accepted, and production cannot select the local mock.

Alpaca risk uses a durable observed period keyed by Alpaca account and USD.
A new account does not inherit previous venue risk, a global peak file, or an
invented weekly result copied from daily P&L. Settings allows an explicit start.
Retries/restarts preserve the baseline and losses; missing/corrupt/suspended
risk history blocks entries while portfolio reads and exits remain available.

## Cutover

Before the new deployment runs, reconcile and retire outstanding state on the
previous venue using the previous deployment or that venue’s own interface.
Do not cancel or close anything merely as a side effect of installing this code.
Use a dedicated Alpaca journal/database if historical foreign positions remain.
Do not copy/relabel foreign orders, positions or IDs into it. Preserve the prior
database for audit. The runtime refuses a journal containing foreign unresolved
intents or open positions without Alpaca entry provenance.

Set `TRAIDO_BROKER=alpaca`, Paper credentials and `ALPACA_DATA_FEED=iex`. Remove
obsolete venue and tunnel environment variables. Apply migrations, inspect the
reported Alpaca account, start its observed risk period, then perform a manually
confirmed Paper lifecycle: entry, protection, partial fill, exit and restart.
Automated tests use fake HTTP responses and do not certify a connected account.
