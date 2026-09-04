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
  /** Passed Stage 0, cut by universe_max_size before market filter. */
  eligible_capped: number;

  market_filter_evaluated: number;
  market_filter_passed: number;
  market_filter_rejected: number;

  quant_evaluated: number;
  quant_shortlisted: number;
  quant_rejected: number;
  /** Scored well enough to survive, not well enough for the Top-K. */
  quant_outranked: number;

  deep_analysis_started: number;
  /** Shortlisted then cut by deep_analysis_top_k before analysis. */
  deep_analysis_outranked: number;
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
    /** Full company name from Finnhub profile2, when available. */
    name?: string | null;
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
    admission_version?: string | null;
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
  /** Card version the operator must echo on APPROVE. */
  decision_version?: number;
  /** True when created before admission control (DB flag). */
  legacy?: boolean;
  creation_admission_record_id?: string | null;
  creation_admission_version?: string | null;
  creation_geometry_hash?: string | null;
};

export type EntryLikelihood = {
  classification: "LOW" | "MODERATE" | "HIGH";
  score: number;
  distance_pct: number | null;
  distance_atr: number | null;
  time_remaining_minutes: number;
  reason_codes: string[];
};

export type EntryWatchCard = {
  id: string;
  symbol: string;
  /** Full company name from Finnhub profile2, when available. */
  name?: string | null;
  status: string;
  ui_state?: "WAITING" | "APPROACHING" | "IN_ZONE" | "TRIGGERED";
  status_label?: string;
  thesis: string;
  setup_type?: string;
  setup_quality?: number | null;
  entry_quality?: number | null;
  entry_quality_at_creation: number;
  setup_quality_at_creation?: number;
  signal_price: string;
  current_price_at_creation: string;
  /** Live tick from the watch loop; falls back to creation price in UI. */
  last_price?: string | null;
  /** Last non-flat move vs previous watch-loop tick: up / down / flat. */
  price_tick?: "up" | "down" | "flat" | null;
  last_observed_at?: string | null;
  created_at?: string;
  entry_zone_low: string;
  entry_zone_high: string;
  /** Printed zone ±0.2 ATR — trigger/admission band (may extend above planned entry). */
  entry_zone_trigger_low?: string | number | null;
  entry_zone_trigger_high?: string | number | null;
  planned_entry: string;
  planned_stop: string;
  planned_target: string;
  planned_risk_reward?: number | null;
  required_conditions: string[];
  reasons?: string[];
  valid_until: string;
  chase_reasons?: string[];
  entry_likelihood?: EntryLikelihood | null;
  distance_to_zone_pct?: number | null;
  distance_to_zone_atr?: number | null;
  zone_arrival_quality?: number | null;
  zone_arrival_type?: string | null;
  arrival_reason_codes?: string[];
  buy_blocked?: boolean;
  /** Human-readable block reason when in zone but not admissible. */
  desk_block_reason?: string | null;
  /** Latest admission/trigger failure while revalidating (spread, R:R, chase…). */
  desk_revalidation_hint?: string | null;
  /** Live top-of-book spread from the watch loop (IEX/SIP). */
  live_spread_bps?: number | null;
  max_spread_bps?: number | null;
  spread_acceptable?: boolean | null;
};

export type AdmissionExplainField = {
  label: string;
  value: string;
  status: "pass" | "warn" | "fail" | "info";
};

export type TradeAdmissionExplain = {
  entity_type: string;
  entity_id: string;
  symbol: string;
  headline: string;
  decision: string;
  admitted: boolean;
  fields: AdmissionExplainField[];
  vetoes: string[];
  reason_codes: string[];
  admission_version: string;
  recorded_at: string | null;
};

export type SellOpportunity = {
  id: string;
  status: string;
  proposal: {
    symbol: string;
    /** Full company name from Finnhub profile2, when available. */
    name?: string | null;
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

export type EntryPolicy = {
  aggressiveness: number;
  label?: string;
  thresholds?: Record<string, number | boolean>;
  note?: string;
  rescan?: { aborted?: boolean; requested?: boolean; cycle?: number; running?: boolean };
};

export type BrokerBackend = {
  backend: "alpaca" | "ibkr" | string;
  environment?: string;
  connection_state?: string;
  account_id?: string | null;
  broker_class?: string;
  switch_blocked_reason?: string | null;
  note?: string;
  error?: string;
};

export type WatchFunnel = {
  waiting?: number;
  triggered?: number;
  admitted?: number;
  in_zone?: number;
  blocked_in_zone?: number;
};

export type AutoTrigger = {
  enabled: boolean;
  note?: string;
};

export type DeskLight = {
  mode: string;
  session?: SessionState;
  entry_policy?: EntryPolicy;
  auto_trigger?: AutoTrigger;
  broker_backend?: BrokerBackend;
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
  watch_funnel?: WatchFunnel;
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

/** Drop cached ETag so the next desk poll cannot keep a stale entry_policy. */
export function invalidateDeskEtag(): void {
  deskEtag = null;
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
  opts?: { requestId?: string; expectedDecisionVersion?: number },
) {
  const body: {
    decision: "approve" | "skip";
    qty?: number;
    request_id?: string;
    expected_decision_version?: number;
  } = { decision };
  if (decision === "approve" && qty != null && Number.isFinite(qty)) {
    body.qty = qty;
  }
  if (decision === "approve") {
    body.request_id = opts?.requestId ?? crypto.randomUUID();
    body.expected_decision_version = opts?.expectedDecisionVersion ?? 0;
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

export async function fetchAdmissionExplain(watchId: string): Promise<TradeAdmissionExplain> {
  const q = new URLSearchParams({ watch_id: watchId });
  const res = await fetch(apiUrl(`/api/v1/admission/explain?${q}`), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(parseApiError(data, res.statusText || "admission_explain_failed"));
  }
  return data as TradeAdmissionExplain;
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

export type LogEventsResponse = {
  events: ActivityEvent[];
  retention_days: number;
  has_more: boolean;
};

export async function fetchLogEvents(params?: {
  limit?: number;
  before?: string;
  agent?: string;
}): Promise<LogEventsResponse> {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.before) q.set("before", params.before);
  if (params?.agent && params.agent !== "all") q.set("agent", params.agent);
  const suffix = q.size ? `?${q.toString()}` : "";
  const res = await fetch(apiUrl(`/api/v1/logs/events${suffix}`), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "logs_failed"));
  return data as LogEventsResponse;
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

export async function fetchEntryPolicy(): Promise<EntryPolicy> {
  const res = await fetch(apiUrl("/api/v1/entry-policy"), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "entry_policy_failed"));
  return data as EntryPolicy;
}

export async function setEntryPolicy(aggressiveness: number): Promise<EntryPolicy> {
  const res = await fetch(apiUrl("/api/v1/entry-policy"), {
    method: "PUT",
    headers: apiHeaders(true),
    body: JSON.stringify({ aggressiveness }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "entry_policy_failed"));
  return data as EntryPolicy;
}

export async function fetchAutoTrigger(): Promise<AutoTrigger> {
  const res = await fetch(apiUrl("/api/v1/auto-trigger"), { headers: apiHeaders(), cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "auto_trigger_failed"));
  return data as AutoTrigger;
}

export async function setAutoTrigger(enabled: boolean): Promise<AutoTrigger> {
  const res = await fetch(apiUrl("/api/v1/auto-trigger"), {
    method: "PUT",
    headers: apiHeaders(true),
    body: JSON.stringify({ enabled }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "auto_trigger_failed"));
  return data as AutoTrigger;
}

export async function fetchBrokerBackend(): Promise<BrokerBackend> {
  const res = await fetch(apiUrl("/api/v1/broker-backend"), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "broker_backend_failed"));
  return data as BrokerBackend;
}

export async function setBrokerBackend(backend: "alpaca" | "ibkr"): Promise<BrokerBackend> {
  const res = await fetch(apiUrl("/api/v1/broker-backend"), {
    method: "PUT",
    headers: apiHeaders(true),
    body: JSON.stringify({ backend }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "broker_backend_failed"));
  return data as BrokerBackend;
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
  strategy: "desk" | "stub" = "desk",
): Promise<EvaluationResult> {
  const qs = new URLSearchParams({
    refresh: String(refresh),
    strategy,
  });
  const res = await fetch(
    apiUrl(`/api/v1/evaluation/${encodeURIComponent(symbol)}?${qs}`),
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

export type StrategyVersion = {
  id: string;
  key: string;
  name: string;
  version_tag: string;
  parameter_hash: string;
  parameters: Record<string, unknown>;
  stage: string;
  evidence: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  approved_at?: string | null;
  approved_by?: string | null;
  rejected_at?: string | null;
  rejected_reason?: string | null;
  notes?: string | null;
};

export type StrategiesPayload = {
  thresholds: Record<string, number>;
  versions: StrategyVersion[];
  production: StrategyVersion[];
};

export async function fetchStrategies(): Promise<StrategiesPayload> {
  const res = await fetch(apiUrl("/api/v1/strategies"), {
    headers: apiHeaders(),
    cache: "no-store",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, "strategies_failed"));
  return data as StrategiesPayload;
}

async function strategyAction(
  id: string,
  action: "recompute" | "approve" | "promote" | "reject",
  body?: Record<string, string>,
): Promise<StrategyVersion> {
  const res = await fetch(apiUrl(`/api/v1/strategies/${encodeURIComponent(id)}/${action}`), {
    method: "POST",
    headers: { ...apiHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body ?? { actor: "operator" }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(parseApiError(data, `strategy_${action}_failed`));
  return (data.version ?? data) as StrategyVersion;
}

export const recomputeStrategy = (id: string) => strategyAction(id, "recompute");
export const approveStrategy = (id: string) => strategyAction(id, "approve");
export const promoteStrategy = (id: string) => strategyAction(id, "promote");
export const rejectStrategy = (id: string, reason: string) =>
  strategyAction(id, "reject", { actor: "operator", reason });

