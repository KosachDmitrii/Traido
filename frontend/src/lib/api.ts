export type AgentState = {
  id: string;
  name: string;
  status: string;
  detail: string;
  last_symbol: string | null;
  score: number | null;
  updated_at: string | null;
  /** Took part in the pass happening right now.
   *
   * Broader than `status === "working"` on purpose: most stages finish between
   * two desk polls, so a bare status would leave every analyst permanently
   * still on screen while it was in fact running. */
  active?: boolean;
};

/** The one status the desk shows — for the label, the dot, and the border alike.
 *
 * Raw `status` is a microsecond-resolution fact sampled every few seconds, so
 * reading it literally paints an analyst as finished while the pass it belongs
 * to is still running. Reading `active` separately is no better: a border around
 * a row that says "Done" claims two contradictory things at once. So there is
 * one state, and everything on screen follows it.
 *
 * Only `done` is upgraded by recent activity, and that is the whole point of the
 * distinction. `done` means a stage finished while the pass carried on, so the
 * agent is still engaged. `idle` and `error` are the agent saying outright that
 * it is not working — a scanner that parked on a full queue, an analyst whose
 * call failed — and a window must never talk over that. It would produce
 * "Working · Cycle 17 paused", which is exactly the contradiction this function
 * exists to prevent.
 */
export function agentDisplayStatus(agent: AgentState): string {
  if (agent.status === "working") return "working";
  if (agent.status === "done" && agent.active === true) return "working";
  return agent.status || "idle";
}

/** Whether to run the marching-ants border for this agent. */
export function isAgentLive(agent: AgentState): boolean {
  return agentDisplayStatus(agent) === "working";
}

export type ActivityEvent = {
  ts: string;
  agent: string;
  message: string;
  symbol: string | null;
  level: string;
};

/**
 * Per-cycle scan funnel — where every instrument in the universe ended up.
 *
 * Mirrors `agents/scanner/funnel.py`. It is a ledger, not a set of highlights:
 * `terminal_total` equals `universe_total` when nothing was lost, and
 * `reconciles` is the backend's own verdict on that.
 */
export type ScanFunnel = {
  universe_total: number;
  structurally_eligible: number;
  stage0_rejected: number;

  market_filter_evaluated: number;
  market_filter_passed: number;
  market_filter_rejected: number;

  quant_evaluated: number;
  quant_shortlisted: number;
  quant_rejected: number;
  /** Scored well enough to survive, not well enough for the Top-K. */
  quant_outranked: number;

  deep_analysis_started: number;
  deep_analysis_passed: number;
  deep_analysis_failed: number;
  deep_analysis_no_candidate: number;

  risk_passed: number;
  risk_rejected: number;

  published: number;
  /** Cleared risk but lost the final ranking — selection, not failure. */
  final_outranked: number;
  capacity_rejected: number;
  duplicate_symbol_rejected: number;

  provider_failed: number;
  data_stale: number;
  ai_budget_exhausted: number;
  /** Never looked at: the book already holds it. */
  position_open: number;

  stage0_reasons: Record<string, number>;
  market_filter_reasons: Record<string, number>;
  quant_reasons: Record<string, number>;
  rejection_reasons: Record<string, number>;

  /** The cycle stopped on a full proposal queue rather than walking the universe. */
  paused_on_full_queue: boolean;

  terminal_total: number;
  unaccounted: number;
  reconciles: boolean;
};

/** Seconds spent in each stage of the last cycle. */
export type ScanStageSeconds = {
  universe?: number;
  market_filter?: number;
  prerank?: number;
  deep_analysis?: number;
  publish?: number;
  total?: number;
};

/** Cadence rather than latency: when the next cycle is due, and whether the last overran. */
export type ScanSchedule = {
  interval_seconds?: number;
  seconds_until_next?: number;
  last_duration_seconds?: number;
  last_overrun_seconds?: number;
  overruns?: number;
};

export type BuyViability = {
  /** live | wide | drifted | past_setup | unverified */
  state: string;
  buyable: boolean;
  reasons: string[];
  measured?: Record<string, unknown>;
  as_of?: string;
};

export type BuyOpportunity = {
  id: string;
  status: string;
  /** When the scanner wrote the card. The prices below are of this moment. */
  created_at?: string;
  candidate: {
    symbol: string;
    confidence: number;
    entry: string;
    stop: string;
    target: string;
    risk_reward: number;
    reasons?: string[];
    thesis?: string | null;
    entry_decision?: string | null;
    entry_quality?: number | null;
    entry_quality_breakdown?: Record<string, number>;
    chase_reasons?: string[];
    signal_price?: string | null;
    entry_zone_low?: string | null;
    entry_zone_high?: string | null;
    target_model?: string | null;
    target_reachability?: string | null;
  };
  risk?: {
    sized_qty?: string | null;
    verdict?: string;
  };
  /** Scan-time size (P1-10). */
  proposed_qty?: string | null;
  /** Size that cleared risk at approve. */
  approved_qty?: string | null;
  /** Filled size after execution. */
  executed_qty?: string | null;
  /** Live book vs the card. Absent only on older payloads; treat missing as unverified. */
  viability?: BuyViability;
};

export type EntryWatchCard = {
  id: string;
  symbol: string;
  status: string;
  thesis: string;
  entry_quality_at_creation: number;
  signal_price: string;
  current_price_at_creation: string;
  entry_zone_low: string;
  entry_zone_high: string;
  planned_entry: string;
  planned_stop: string;
  planned_target: string;
  required_conditions: string[];
  reasons?: string[];
  valid_until: string;
  chase_reasons?: string[];
};

export type SellOpportunity = {
  id: string;
  status: string;
  proposal: {
    symbol: string;
    entry: string;
    current: string;
    pnl_pct: number;
    reasons: string[];
    confidence: number;
  };
};

export type DeskPosition = {
  symbol: string;
  /** Full company name from Finnhub profile2, when available. */
  name?: string | null;
  qty: string;
  avg_entry: string;
  stop?: string | null;
  target?: string | null;
  strategy_version?: string | null;
  /** The broker's own mark. Null when it did not report one — not zero. */
  mark?: string | null;
  pnl?: string | null;
  pnl_pct?: number | null;
};

export type DeskOpenOrder = {
  broker_order_id?: string | null;
  client_order_id?: string | null;
  symbol: string;
  side: string;
  order_type: string;
  qty: string;
  filled_qty?: string;
  status: string;
  limit_price?: string | null;
  stop_price?: string | null;
};

/** Light desk — local stores only (no Alpaca). */
export type ReviewPayload = {
  trade_count: number;
  win_rate: number;
  expectancy?: number | null;
  notes?: string[];
  recent?: Array<{
    id?: string;
    symbol: string;
    /** Full company name from Finnhub profile2, when available. */
    name?: string | null;
    entry?: string;
    exit?: string;
    pnl: string;
    pnl_pct: number;
    strategy_version?: string;
  }>;
};

/** The US equity session as the RTH gate sees it, with the clock it judged. */
export type SessionState = {
  phase: "regular" | "premarket" | "after_hours" | "closed_weekend" | "closed_holiday" | string;
  /** New entries only. Protective exits and reconciliation run in every phase. */
  entries_allowed: boolean;
  et_time: string;
  /** Exchange-local date, preformatted server-side so the browser never has to
   *  re-derive which day it is in New York. */
  et_date: string;
  opens_at: string;
  /** 13:00 on an early-close day, so this is read rather than assumed. */
  closes_at: string;
};

export type DeskLight = {
  mode: string;
  session?: SessionState;
  scanner: {
    enabled?: boolean;
    running?: boolean;
    cycle?: number;
    last_symbol?: string | null;
    last_finished_at?: string | null;
    symbols_scanned?: number;
    opportunities_found?: number;
    universe?: string[];
    error?: string | null;
    funnel?: ScanFunnel;
    stage_seconds?: ScanStageSeconds;
    schedule?: ScanSchedule;
    /** Names that reached the quant Top-K, in ranked order. */
    shortlist?: string[];
    ai_budget?: Record<string, number>;
    provider_stats?: Record<string, Record<string, number>>;
  };
  buy_opportunities: BuyOpportunity[];
  entry_watches?: EntryWatchCard[];
  sell_opportunities: SellOpportunity[];
  positions: DeskPosition[];
  review: ReviewPayload;
  activity: {
    agents: AgentState[];
    events: ActivityEvent[];
  };
  message: string;
  rev?: number;
};

export type BrokerSnapshot = {
  portfolio?: {
    equity: string;
    cash: string;
    buying_power?: string;
    day_pnl: string;
    open_positions: number;
    open_orders?: number;
  } | null;
  positions: DeskPosition[];
  open_orders: DeskOpenOrder[];
  /** Whether the desk is still able to check its book against the broker. */
  reconciliation?: {
    ok: boolean | null;
    error: string | null;
    last_success_at: number | null;
    stale_seconds: number | null;
  };
  rev?: number;
  cached?: boolean;
  as_of?: number | null;
  /** How long the server will serve this snapshot before rebuilding it. */
  ttl_seconds?: number | null;
};

/** Merged view for existing desk components. */
export type DeskResponse = DeskLight & {
  open_orders?: DeskOpenOrder[];
  portfolio?: BrokerSnapshot["portfolio"];
  reconciliation?: BrokerSnapshot["reconciliation"];
  broker_ttl_seconds?: number | null;
};

import { apiUrl, getStoredApiKey, streamUrl } from "./config";
import { parseApiError } from "./messages";

function apiHeaders(json = false): Record<string, string> {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  const key = getStoredApiKey();
  if (key) h["X-API-Key"] = key;
  return h;
}

let deskEtag: string | null = null;

export function mergeDesk(light: DeskLight, broker: BrokerSnapshot | null): DeskResponse {
  return {
    ...light,
    portfolio: broker?.portfolio ?? null,
    positions: broker?.positions?.length ? broker.positions : light.positions,
    open_orders: broker?.open_orders ?? [],
    reconciliation: broker?.reconciliation,
    broker_ttl_seconds: broker?.ttl_seconds ?? null,
  };
}

/** Returns null on 304 Not Modified. */
export async function fetchDeskLight(signal?: AbortSignal): Promise<DeskLight | null> {
  const headers = apiHeaders();
  if (deskEtag) headers["If-None-Match"] = deskEtag;
  const res = await fetch(apiUrl("/api/v1/desk"), {
    headers,
    cache: "no-store",
    signal,
  });
  if (res.status === 304) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "desk_failed"));
  }
  const etag = res.headers.get("ETag");
  if (etag) deskEtag = etag;
  return data as DeskLight;
}

export async function fetchBroker(fresh = false): Promise<BrokerSnapshot> {
  const q = fresh ? "?fresh=1" : "";
  const res = await fetch(apiUrl(`/api/v1/desk/broker${q}`), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "broker_failed"));
  }
  return data as BrokerSnapshot;
}

/** @deprecated use fetchDeskLight + fetchBroker */
export async function fetchDesk(): Promise<DeskResponse> {
  const [light, broker] = await Promise.all([
    fetchDeskLight(),
    fetchBroker(false).catch(() => null),
  ]);
  if (!light) {
    throw new Error("desk_not_modified_without_local_state");
  }
  return mergeDesk(light, broker);
}

export async function decideBuy(
  id: string,
  decision: "approve" | "skip",
  qty?: number,
) {
  const body: { decision: "approve" | "skip"; qty?: number } = { decision };
  if (decision === "approve" && qty != null && Number.isFinite(qty)) {
    body.qty = qty;
  }
  const res = await fetch(apiUrl(`/api/v1/opportunities/${id}/decide`), {
    method: "POST",
    headers: apiHeaders(true),
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "decide_failed"));
  }
  return data;
}

export async function decideSell(id: string, decision: "sell" | "hold") {
  const res = await fetch(apiUrl(`/api/v1/exits/${id}/decide`), {
    method: "POST",
    headers: apiHeaders(true),
    body: JSON.stringify({ decision }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "exit_failed"));
  }
  return data;
}

/** Flatten a position on demand, without waiting for an agent to propose it. */
export async function closePosition(symbol: string) {
  const res = await fetch(apiUrl(`/api/v1/positions/${encodeURIComponent(symbol)}/close`), {
    method: "POST",
    headers: apiHeaders(true),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "close_failed"));
  }
  return data;
}

export async function runScanner() {
  await fetch(apiUrl("/api/v1/scanner/run"), {
    method: "POST",
    headers: apiHeaders(),
  }).catch(() => undefined);
}

export function subscribeDeskEvents(
  onEvent: (ev: { type?: string; channel?: string }) => void,
): () => void {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    return () => undefined;
  }
  const es = new EventSource(streamUrl("/api/v1/desk/stream"));
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as { type?: string; channel?: string });
    } catch {
      /* ignore malformed */
    }
  };
  es.onerror = () => {
    // Browser will retry; keep silent
  };
  return () => es.close();
}

export async function fetchReview(liveOnly = true): Promise<ReviewPayload> {
  const res = await fetch(apiUrl(`/api/v1/review?live_only=${liveOnly ? "true" : "false"}`), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "review_failed"));
  return data as ReviewPayload;
}

export async function fetchKillSwitch(): Promise<{ enabled: boolean }> {
  const res = await fetch(apiUrl("/api/v1/kill-switch"), { headers: apiHeaders(), cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "kill_switch_failed"));
  return data as { enabled: boolean };
}

export async function setKillSwitch(enabled: boolean): Promise<{ enabled: boolean }> {
  const res = await fetch(apiUrl("/api/v1/kill-switch"), {
    method: "POST",
    headers: apiHeaders(true),
    body: JSON.stringify({ enabled }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "kill_switch_failed"));
  return data as { enabled: boolean };
}

export type RegimeResult = {
  regime: string;
  bars: number;
  trade_count: number;
  return_pct: number;
  win_rate: number;
  profit_factor: number | null;
  max_drawdown_pct: number;
};

export type EvaluationResult = {
  symbol: string;
  /** Full company name from Finnhub profile2, when available. */
  name?: string | null;
  timeframe: string;
  generated_at: string;
  bars: number;

  trade_count: number;
  return_pct: number;
  win_rate: number;
  profit_factor: number | null;
  max_drawdown_pct: number;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  cagr_pct: number | null;
  expectancy_r: number | null;
  total_costs: number;
  gross_return_pct: number;

  oos_trade_count: number;
  oos_return_pct: number;
  oos_win_rate: number;
  oos_profit_factor: number | null;
  oos_max_drawdown_pct: number;
  oos_sharpe: number | null;
  walk_forward_efficiency: number | null;
  verdict: string;

  benchmark_symbol: string;
  benchmark_return_pct: number;
  benchmark_max_drawdown_pct: number;
  excess_return_pct: number;
  beats_benchmark: boolean;

  by_regime: RegimeResult[];
  warnings: string[];
};

export async function fetchEvaluation(
  symbol: string,
  refresh = false,
): Promise<EvaluationResult> {
  const res = await fetch(
    apiUrl(`/api/v1/evaluation/${encodeURIComponent(symbol)}?refresh=${refresh}`),
    { headers: apiHeaders(), cache: "no-store" },
  );
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "evaluation_failed"));
  return data as EvaluationResult;
}

export type F3Diagnostics = {
  generated_at?: string;
  signal_quality: {
    shadow_samples: number;
    avg_entry_quality: number | null;
    session_cohorts: Record<string, number>;
    old_policy_counts: Record<string, number>;
    new_policy_counts: Record<string, number>;
    top_chase_reasons: [string, number][];
  };
  target_quality: {
    historical_mfe_samples: { total?: number; by_source?: Record<string, number> };
    min_samples_for_reachability: number;
    reachability_ready: boolean;
  };
  wait_effectiveness: {
    open_watches: number;
    watch_status_counts: Record<string, number>;
    old_buy_to_wait: number;
    old_buy_to_no_trade: number;
    both_buy_now: number;
    wait_rate_vs_old_buy_pct: number | null;
  };
  forward_paper: {
    note: string;
    shadow_samples: number;
    rth_shadow_samples: number;
    target_rth_samples: number;
    progress_pct: number;
    claim_fixed: boolean | null;
  };
};

export async function fetchF3Diagnostics(): Promise<F3Diagnostics> {
  const res = await fetch(apiUrl("/api/v1/diagnostics/f3"), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "f3_diagnostics_failed"));
  return data as F3Diagnostics;
}
