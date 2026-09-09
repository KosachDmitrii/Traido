# Observation policy audit — 2026-09-09

Source: production ScannerTrace, scan `52ae932e-7bfc-4e28-b3f1-fa60f4b91efd`, 19:09:39–19:11:51 UTC, main `59aa400`. All 40 deep outcomes are preserved in `backend/tests/fixtures/observation_scan_20260909.json`.

## Findings

- 22 candidates stopped at structure; 1 at the separate 2.0 desk R:R gate (CRM: entry 245.02, stop 240.22, target 254.29; displayed RR 1.93).
- Seven had WAIT admission, but unavailable weekly PnL/drawdown stopped watch creation: B, CNQ, AAPL, MSFT, WFC, MPC, IGV. This is not proof that all seven pass the remaining event/news prerequisites: the old risk engine returned early on missing account history.
- Ten other candidates had terminal admission outcomes. Reasons overlap.
- The archived logs contain computed timing facts for later-stage candidates, but not original candles or all D1/H4 numerical inputs for early rejects. Historical raw-input recalculation of all 40 is therefore unavailable. No candles or missing prices have been fabricated.

## Authorized observation changes

The user explicitly requested separation of WAIT from account readiness, a review of EMA/H4 and R:R gates, and same-input comparison. `observation@2` implements:

- A known D1 uptrend may be observed while slow EMA confirmation is absent; a D1 uptrend/range with bullish EMA may be observed through an H4 downtrend. D1 downtrend, unknown structure, and range without bullish EMA remain rejected.
- These plans carry durable `DESK_STRUCTURE_CONFIRMATION`, which must pass fresh D1/H4 checks at watch conversion and final approval. Observation does not change the strict structure policy.
- Zone geometry is derived before desk R:R evaluation. The observation floor uses the existing 1.45 base geometry floor. A plan under the prior 2.0 desk floor carries `DESK_RR_CONFIRMATION`; no target/stop is stretched to make it pass. Final effective-R:R rules remain in force.
- Observation context reads real news, calendar, sector and regime. It neither reads/sizes against account equity nor grants RiskVerdict.PASS. Full capital risk is repeated at conversion and approval. Unknown weekly PnL/drawdown remains a BUY rejection.
- Unread news is no longer labelled CHECKED on the WAIT path.

## Reproducibility and outcomes

Every new desk evaluation records compressed original Alpaca candles with SHA-256, features, levels, reasons and the baseline/observation structure comparison. Features are immediately recomputed from the captured inputs and compared. No second vendor fetch is used for this comparison.

Watch shadow records retain the policy cohort and track subsequent sampled-price MFE/MAE and zone arrival, including negative movement. These are observed excursions, not executed trade PnL. `trading.observation_replay.evaluate_forward_path` can evaluate later OHLC bars; pre-decision bars are excluded, and ambiguous same-bar entry/stop/target ordering is reported rather than resolved optimistically.

No profitable strategy conclusion is supported yet. A complete future observation window cannot be manufactured during a code change. The old 40-ticker trace is not advertised as a full historical backtest.

## All 40 candidates

| Ticker | Recorded outcome | Recorded rejection codes |
|---|---|---|
| SMCI | no_candidate | STRUCTURE_REJECT |
| B | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE |
| MRK | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| T | no_candidate | STRUCTURE_REJECT |
| VZ | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC |
| CNQ | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE |
| SLV | no_candidate | STRUCTURE_REJECT |
| AAPL | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, BUY_CONFIRMATION_RELAXED, BUY_ALLOWED, WAITING_CONFIRMATION |
| DVN | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, CANDIDATE_SETUP_BELOW_FLOOR, NOT_BUY_READY |
| PFE | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, EXTREME_CHASE, TARGET_UNREALISTIC |
| NU | no_candidate | STRUCTURE_REJECT |
| XLE | no_candidate | STRUCTURE_REJECT |
| OXY | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, CANDIDATE_SETUP_BELOW_FLOOR, NOT_BUY_READY |
| MSFT | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE, EXTREME_CHASE |
| JNJ | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| BKR | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC |
| JPM | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC |
| NVDA | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC |
| XLK | no_candidate | STRUCTURE_REJECT |
| MDLZ | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC |
| HPQ | no_candidate | STRUCTURE_REJECT |
| WFC | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE |
| HWM | no_candidate | STRUCTURE_REJECT |
| AMAT | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| HPE | no_candidate | STRUCTURE_REJECT |
| NOK | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| MPC | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE, NOT_BUY_READY |
| DRAM | no_candidate | STRUCTURE_REJECT |
| IEFA | no_trade | TARGET_UNREALISTIC, STRUCTURAL_DAMAGE, NO_PULLBACK_PATH |
| IGV | no_trade | WEEKLY_PNL_UNAVAILABLE, PORTFOLIO_DRAWDOWN_UNAVAILABLE, ENTRY_OUTSIDE_ALLOWED_ZONE |
| CAT | no_candidate | STRUCTURE_REJECT |
| MCD | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| TSLA | no_candidate | STRUCTURE_REJECT |
| CRM | no_candidate | RISK_PLAN_RR_LOW |
| INTC | no_trade | ENTRY_OUTSIDE_ALLOWED_ZONE, TARGET_UNREALISTIC, ARRIVAL_TYPE_GAP_DOWN |
| ACN | no_candidate | STRUCTURE_REJECT |
| CMCSA | no_candidate | STRUCTURE_REJECT |
| PG | no_candidate | STRUCTURE_REJECT |
| RSP | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |
| BKNG | no_candidate | STRUCTURE_H4_CONFLICT, STRUCTURE_REJECT |


## Production follow-up: completed observation cycle and nearest levels

Deployment `1d7ad1c` completed cycle `b811d0ce-14b8-41c2-9af9-8df7733fe83a`
at 19:49:37 UTC: 40 deep candidates completed, 5 WAIT (CNQ, HCA, AAPL,
XLE, MSFT), zero operational failures. All 40 input audits passed numerical
replay. This is discovery evidence, not evidence of profitable executions.

Inspecting the actual levels exposed a separate semantic defect that a same-code
replay cannot catch: support/resistance truncation retained the highest three
clusters before filtering against current price. SMCI at 39.16 had reported D1
supports 50.34, 51.635, 52.59; B at 44.62 had H1 supports above price too.
Downstream filtering discarded those levels, losing nearer valid supports.
Production features now select supports below and resistances above the latest
close BEFORE keeping the nearest three. No missing level is fabricated.

AAPL screenshots with `ZONE_ARRIVAL_QUALITY_LOW:17<35` concern BUY confirmation,
not WAIT admission. In arrival@1, UNKNOWN/no pullback path starts at 32 and
the volume/red-bar penalty subtracts 15. The threshold 35 is a configurable
internal policy, not an exchange requirement. A tolerance-band touch alone
does not certify arrival quality. Logs subsequently show AAPL BUY_ALLOWED at
19:57:54 UTC; that cycle still reports account risk history unavailable.
Arrival confirmation and account readiness are separate gates.

WAIT store refreshes can replace geometry on a new scanner plan while WAITING;
TRIGGERED/REVALIDATING records are not replaced by that path. The two screenshots
lack watch IDs and revision timestamps, so they cannot establish whether they
show one record changing or two successive plans.
