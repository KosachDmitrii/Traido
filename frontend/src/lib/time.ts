/** Format API timestamps in US equities exchange time (America/New_York). */

export const EXCHANGE_TZ = "America/New_York";

function parseTs(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** HH:MM:SS on the exchange clock. */
export function formatExchangeTime(iso: string | null | undefined): string {
  const d = parseTs(iso);
  if (!d) {
    if (!iso) return "—";
    return iso.slice(11, 19) || "—";
  }
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: EXCHANGE_TZ,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

/** e.g. Sep 1, 07:00:43 — exchange date + time. */
export function formatExchangeDateTime(iso: string | null | undefined): string {
  const d = parseTs(iso);
  if (!d) return iso || "—";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: EXCHANGE_TZ,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

/** Compact stamp for dense rows: `Sep 1 07:00:43`. */
export function formatExchangeStamp(iso: string | null | undefined): string {
  const d = parseTs(iso);
  if (!d) return iso || "—";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: EXCHANGE_TZ,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(d);
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${get("month")} ${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
}

/** Aliases used across the desk UI (all exchange-local). */
export const formatLocalTime = formatExchangeTime;
export const formatLocalDateTime = formatExchangeDateTime;
