# Production readiness — 2026-09-10

Status: **PAPER_TESTING_ONLY**. Alpaca Paper is the only execution provider.
Data uses Alpaca IEX in development; live trading remains disabled.

The Alpaca-only change was verified locally with 1462 passing backend tests
(one existing skip), 107 focused safety/adapter/risk tests, Ruff lint and format,
mypy for 150 source files, and a frontend production build. These are automated
checks with fake transports, not evidence of a connected trading session.

Before a Paper deployment is usable, configure Paper credentials, reconcile
previous operational state, verify the reported Alpaca account, explicitly start
its observed risk period, and validate a manually confirmed entry, protection,
partial fill, exit and restart. Do not reset a risk period to erase losses.

See [deployment](deploy/railway.md), [cutover](architecture/alpaca-only.md),
[execution failure matrix](architecture/execution-failure-matrix.md), and
[architecture](../ARCHITECTURE.md). Prior audit snapshots remain in Git history.
