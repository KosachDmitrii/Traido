# Database Schema Proposal (V1)

PostgreSQL 16 · SQLAlchemy 2 · Alembic  
Money fields: `Numeric(18, 8)` for prices/qty where needed; P&L `Numeric(18, 4)` in account currency.  
Timestamps: `timestamptz`. Soft deletes avoided for money tables — use status enums.

## Tables

### `pipeline_runs`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | `pipeline_run_id` |
| started_at / finished_at | timestamptz | |
| status | enum | running, completed, failed |
| symbols_scanned | int | |
| error_summary | text null | |

### `audit_events` (append-only)
| Column | Type | Notes |
|--------|------|-------|
| id | bigserial PK | |
| created_at | timestamptz | default now() |
| pipeline_run_id | UUID null FK | |
| event_type | text | see event-flow.md |
| actor | text | agent/service/user/system |
| entity_type / entity_id | text null | |
| payload | jsonb | immutable snapshot |
| integrity_hash | text null | optional chain hash later |

**No UPDATE/DELETE** from application code. DB role: insert-only recommended for this table.

### `market_bars`
| Column | Type | Notes |
|--------|------|-------|
| id | bigserial | |
| symbol | text | |
| timeframe | text | 1D, 4H, 1H, 15m, … |
| ts | timestamptz | bar open |
| open/high/low/close | numeric | |
| volume | numeric | |
| source | text | provider id |
| UNIQUE(symbol, timeframe, ts, source) | | |

### `feature_snapshots`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| pipeline_run_id | UUID | |
| symbol | text | |
| timeframe | text | |
| computed_at | timestamptz | |
| features | jsonb | indicators + pattern flags |

### `assessments`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| pipeline_run_id | UUID | |
| symbol | text | |
| kind | enum | technical, news, market, quant |
| score | numeric null | 0–100 |
| label | text null | |
| payload | jsonb | full schema dump |
| model_name / prompt_version | text null | for LLM kinds |

### `trade_candidates`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| pipeline_run_id | UUID | |
| symbol | text | |
| action | enum | buy (sell reserved for exits) |
| confidence | numeric | 0–1 |
| entry / stop / target | numeric | |
| risk_reward | numeric | |
| reasons | jsonb | string[] |
| strategy_version | text | |
| created_at | timestamptz | |

### `risk_decisions`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| candidate_id | UUID FK | |
| decision | enum | pass, reject |
| reasons | jsonb | |
| sized_qty | numeric null | |
| max_loss_usd | numeric null | |
| portfolio_snapshot | jsonb | equity, exposure at decision |
| created_at | timestamptz | |

### `opportunities`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| candidate_id | UUID | |
| risk_decision_id | UUID | |
| status | enum | awaiting_confirmation, approved, skipped, expired, executed, discarded |
| expires_at | timestamptz null | |
| user_decision_at | timestamptz null | |
| user_decision | enum null | approve, skip |

### `orders`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| opportunity_id | UUID null | |
| position_id | UUID null | |
| broker | text | |
| broker_order_id | text null | |
| client_order_id | text UNIQUE | idempotency |
| symbol | text | |
| side | enum | buy, sell |
| order_type | enum | market, limit, stop, stop_limit |
| qty | numeric | |
| limit_price / stop_price | numeric null | |
| status | enum | submitted, accepted, partial, filled, canceled, rejected |
| raw | jsonb | broker payload |

### `fills`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| order_id | UUID | |
| qty / price | numeric | |
| filled_at | timestamptz | |
| fees | numeric | |
| raw | jsonb | |

### `positions`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| symbol | text | |
| qty | numeric | |
| avg_entry | numeric | |
| stop_price / target_price | numeric null | |
| status | enum | open, closed |
| opened_at / closed_at | timestamptz | |
| realized_pnl | numeric null | |

Partial unique index `ux_open_positions_one_open_per_symbol` on `(symbol) WHERE
status = 'open'`. One open position per symbol is a capital-safety invariant,
not a convenience: a broker reports one net position per symbol, so two open
rows can never both be reconciled against it, and each would carry its own
protective stop for shares the other also claims. Partial rather than plain
because closed rows for a re-traded symbol accumulate and must not collide.

### `trade_journal`
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | |
| position_id | UUID | |
| pipeline_run_id | UUID null | |
| symbol | text | |
| entry / exit | numeric | |
| qty | numeric | |
| pnl / pnl_pct | numeric | |
| mfe / mae | numeric null | |
| max_drawdown_during | numeric null | |
| entry_reasons / exit_reasons | jsonb | |
| indicators_at_entry | jsonb | |
| assessments_at_entry | jsonb | |
| market_regime | text null | |
| news_context | jsonb null | |
| strategy_version / prompt_version | text | |
| trading_mode | text | confirmation / autopilot |

### `portfolio_snapshots`
Daily (or per-change) equity curve for metrics.

### `app_settings`
Key/value JSON for `trading.mode`, risk limits, kill_switch.

## Indexes (minimum)
- `audit_events (pipeline_run_id, created_at)`
- `market_bars (symbol, timeframe, ts DESC)`
- `opportunities (status, created_at)`
- `orders (client_order_id)` UNIQUE
- `positions (status, symbol)`

## Migrations
Alembic from day of Stage 1. No LLM-generated migration SQL applied without human review.
