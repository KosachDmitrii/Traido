# ORB 2.4 / 2.5 — calibrated entry, stop and intraday tape filter

Status: **paused for new plans (owner decision 2026-10-23).** Two confirmed
setups and no entries across the 2026-10-22/23 sessions: the wider stop
against an unchanged observed-peak target fails the 1.5 R:R gate almost
always. New plans use 2.2/2.3 again; persisted 2.4/2.5 data keeps these
parameters. Reintroduce one parameter at a time.

Experimental Paper implementation, not statistically validated.
Versions: `orb@2.4.0` (09:30 opening range) and `orb@2.5.0` (rolling intraday
ranges). Authorized by the owner on 2026-10-21. Breakout, retest, confirmation,
target (observed pre-retest peak, PDH cap) and every exit rule are inherited
from 2.2/2.3 unchanged; this revision changes only where the entry is priced,
where the stop sits, how size is charged and which tape a long may be taken in.
Persisted 2.2/2.3 plans and positions keep their own parameters
(`strategy.orb.parameters_for`).

## Why

| 2.2/2.3 behaviour | Problem | 2.4/2.5 |
|---|---|---|
| Entry only at ≤ level + 0.05 ATR after confirmation | Fills only when price falls back to the level again — the weaker half of the setups (adverse selection) | Buy the confirmation: cap = close + 0.10 R |
| Stop = retest low − 0.02 ATR | Inside five-minute noise; exit slippage can exceed 1 R | Buffer 0.10 daily ATR; stop must be ≥ 0.5 M5 ATR and ≥ 3 spreads away, ≤ 1.0 daily ATR |
| Size from entry − stop | Understates the loss filled below a stop | Size from limit − stop + 1 spread |
| Macro (DGS10, UNRATE) only | Says nothing about today's tape | SPY above session VWAP (two closes), not down 1% on the day; symbol leads SPY from the open and is above its own VWAP |
| Ten-minute signal | Long for an entry priced at the confirmation | One bar (five minutes) |

## Parameters (`CALIBRATED_PARAMETERS`)

| Key | Value |
|---|---|
| `entry_cap_r` | 0.10 |
| `entry_limit_spreads` | 2 |
| `retest_stop_buffer_atr` | 0.10 |
| `stop_buffer_spreads` | 2 (spread × 2 ≤ buffer) |
| `min_stop_intraday_atr` / `intraday_atr_bars` | 0.5 / 14 |
| `min_stop_spreads` | 3 |
| `max_stop_daily_atr` | 1.0 |
| `sizing_slippage_spreads` | 1 |
| `signal_ttl_minutes` | 5 |
| `market_benchmark` / `market_vwap_closes` / `market_max_drop_pct` | SPY / 2 / 1.0 |

The R:R gate (≥ 1.5 after 10 bps costs) is unchanged and is evaluated at the
cap, the worst permitted fill. Setups that only passed because 2.2 priced
the entry at the level are now rejected; that is intended.

The macro gate is unchanged: its thresholds already act only at extremes.
Whether to retire it is an open decision for the owner.

## Validation

The values above are practitioner priors fixed before testing, not fitted.
Paper evaluation (`GET /api/v1/evaluation/orb-paper`) separates 2.4/2.5 from the
2.2/2.3 `baseline`. Acceptance: positive net expectancy in R over at least
200 Paper trades per version. Change one parameter at a time after that.
