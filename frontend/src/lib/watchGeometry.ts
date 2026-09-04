import type { EntryWatchCard } from "@/lib/api";

export function waitPrice(w: EntryWatchCard): number {
  const raw = w.last_price ?? w.current_price_at_creation;
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

export function watchStrictInZone(w: EntryWatchCard): boolean {
  const price = waitPrice(w);
  const lo = Number(w.entry_zone_low);
  const hi = Number(w.entry_zone_high);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return false;
  return price >= lo && price <= hi;
}

/** Printed zone ± trigger ATR cushion (matches backend price_in_zone). */
export function watchInTriggerBand(w: EntryWatchCard): boolean {
  const price = waitPrice(w);
  const lo = Number(w.entry_zone_trigger_low ?? w.entry_zone_low);
  const hi = Number(w.entry_zone_trigger_high ?? w.entry_zone_high);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return watchStrictInZone(w);
  return price >= lo && price <= hi;
}

/** Match SessionDecisionStrip + backend derive_ui_state (ATR cushion included). */
export function watchInOrNearZone(w: EntryWatchCard): boolean {
  const ui = w.ui_state ?? w.status_label;
  if (ui === "IN_ZONE" || ui === "TRIGGERED") return true;
  const atr = w.distance_to_zone_atr;
  return typeof atr === "number" && Number.isFinite(atr) && atr <= 0.2;
}

export function watchAboveStrictZone(w: EntryWatchCard): boolean {
  const price = waitPrice(w);
  const hi = Number(w.entry_zone_high);
  return Number.isFinite(hi) && price > hi && watchInTriggerBand(w);
}

export function watchBelowStrictZone(w: EntryWatchCard): boolean {
  const price = waitPrice(w);
  const lo = Number(w.entry_zone_low);
  return Number.isFinite(lo) && price < lo && watchInTriggerBand(w);
}

export function watchApproaching(w: EntryWatchCard): boolean {
  if (watchInOrNearZone(w)) return false;
  const ui = w.ui_state ?? w.status_label;
  if (ui === "APPROACHING") return true;
  const atr = w.distance_to_zone_atr;
  return typeof atr === "number" && Number.isFinite(atr) && atr <= 0.5;
}

/** Lower = closer to BUY — rail order: buy → trigger → in zone → near → waiting. */
export function watchPipelineSortRank(w: EntryWatchCard): number {
  const machine = (w.status || "").toLowerCase();
  if (
    machine === "triggered" ||
    machine === "revalidating" ||
    machine === "admitted" ||
    machine === "converting"
  ) {
    return 1;
  }
  const ui = w.ui_state ?? w.status_label;
  if (ui === "TRIGGERED") return 1;
  if (watchInOrNearZone(w)) return 2;
  if (watchApproaching(w) || ui === "APPROACHING") return 3;
  return 4;
}

/** Secondary sort — nearer to the zone first within the same stage. */
export function watchDistanceTie(w: EntryWatchCard): number {
  const atr = w.distance_to_zone_atr;
  return typeof atr === "number" && Number.isFinite(atr) ? atr : 999;
}

export function watchShowsTriggerBand(w: EntryWatchCard): boolean {
  const lo = Number(w.entry_zone_trigger_low);
  const hi = Number(w.entry_zone_trigger_high);
  const planLo = Number(w.entry_zone_low);
  const planHi = Number(w.entry_zone_high);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return false;
  return Math.abs(lo - planLo) > 1e-6 || Math.abs(hi - planHi) > 1e-6;
}
