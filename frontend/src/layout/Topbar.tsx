import { CircleSlash, Clock, ShieldAlert } from "lucide-react";
import type { CSSProperties } from "react";
import type { SessionState } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";

const DAY_MINUTES = 24 * 60;

/** "HH:MM" as minutes past midnight, or null if the server sent something else. */
function clockMinutes(hhmm: string | undefined): number | null {
  const parts = /^(\d{1,2}):(\d{2})$/.exec(hhmm ?? "");
  return parts ? Number(parts[1]) * 60 + Number(parts[2]) : null;
}

/** How far through the current phase, and how many minutes are left of it.
 *
 * Derived only from the three clock strings the server already sent, never
 * from the browser's clock. Mixing the two would let the ring drift out of
 * agreement with the phase label beside it, and the phase is the one the RTH
 * gate will enforce. */
function sessionProgress(s: SessionState): { fraction: number; remaining: number | null } {
  const now = clockMinutes(s.et_time);
  const opens = clockMinutes(s.opens_at);
  const closes = clockMinutes(s.closes_at);
  const idle = { fraction: 0, remaining: null };
  if (now === null || opens === null || closes === null) return idle;

  const between = (from: number, to: number) =>
    to > from
      ? {
          fraction: Math.min(1, Math.max(0, (now - from) / (to - from))),
          remaining: Math.max(0, to - now),
        }
      : idle;

  switch (s.phase) {
    // Pre-market starts at midnight here because that is where the gate starts
    // it: `session_phase` calls any weekday moment before the open pre-market.
    case "premarket":
      return between(0, opens);
    case "regular":
      return between(opens, closes);
    case "after_hours":
      return between(closes, DAY_MINUTES);
    default:
      return idle;
  }
}

/** A gap an operator can act on without doing the subtraction themselves. */
function humanGap(mins: number): string {
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** What the session means for the operator, not what the calendar calls it.
 *
 * Phrased as a consequence because that is the only reason this is on screen:
 * the RTH gate refuses new entries outside the regular session, so a queue of
 * proposals during a weekend is a queue nobody can act on. Exits and
 * reconciliation are deliberately not gated, which is why "closed" never says
 * the desk is asleep. */
function sessionCopy(s: SessionState, remaining: number | null): { title: string; detail: string } {
  // The absolute time stays alongside the countdown rather than replacing it:
  // "in 54m" is what you act on, "09:30 ET" is what you verify it against.
  const gap = remaining !== null && remaining > 0 ? humanGap(remaining) : null;
  switch (s.phase) {
    case "regular":
      return {
        title: "Market open",
        detail: gap
          ? `Entries close in ${gap} · ${s.closes_at} ET`
          : `Entries allowed until ${s.closes_at} ET`,
      };
    case "premarket":
      return {
        title: "Pre-market",
        detail: gap
          ? `Entries open in ${gap} · ${s.opens_at} ET`
          : `Entries open at ${s.opens_at} ET`,
      };
    case "after_hours":
      return { title: "After hours", detail: `Entries closed at ${s.closes_at} ET` };
    case "closed_holiday":
      return { title: "Market holiday", detail: "No entries today · exits still run" };
    case "closed_weekend":
      return { title: "Weekend", detail: "No entries until Monday · exits still run" };
    default:
      return { title: "Session unknown", detail: "Entries gated by the server" };
  }
}

/** Tone by phase, so the badge reads before it is read. Muted on purpose: this
 * sits on screen all day, and a saturated header trains the eye to skip it. */
const PHASE_TONE: Record<string, string> = {
  regular: "open",
  premarket: "soon",
  after_hours: "late",
};

export function Topbar() {
  const { desk, killSwitch } = useDesk();
  const session = desk?.session;
  const { fraction, remaining } = session
    ? sessionProgress(session)
    : { fraction: 0, remaining: null };
  const copy = session ? sessionCopy(session, remaining) : null;
  const tone = session ? PHASE_TONE[session.phase] : undefined;
  const live = (desk?.mode || "").toLowerCase() === "live";

  return (
    <header className="topbar">
      <div className="tb-brand">
        <span className="tb-brand__mark" />
        Traido
      </div>

      <div className={`tb-session${tone ? ` tb-session--${tone}` : ""}`}>
        {/* The ring is the trading day itself: how much of the current phase
            has run, drawn from the same clock that decides the phase. */}
        <span className="tb-dial" style={{ "--dial": String(fraction) } as CSSProperties}>
          <svg className="tb-dial__ring" viewBox="0 0 28 28" aria-hidden>
            <circle className="tb-dial__track" cx="14" cy="14" r="12.5" />
            <circle className="tb-dial__arc" cx="14" cy="14" r="12.5" />
          </svg>
          <Clock size={13} strokeWidth={2} absoluteStrokeWidth aria-hidden />
        </span>
        <span className="tb-session__now">
          <span className="tb-session__clock mono">{session?.et_time ?? "--:--"}</span>
          <span className="tb-session__zone">NY · {session?.et_date ?? "—"}</span>
        </span>
        <span className="tb-session__text">
          <strong>{copy?.title ?? "Loading session…"}</strong>
          <span>{copy?.detail ?? ""}</span>
        </span>
      </div>

      <div className="tb-spacer" />

      {/* Only shown when something is wrong. A row of green "all clear" badges
          trains the eye to skip the strip, which is the one place a real
          warning has to land. */}
      {killSwitch === "on" ? (
        <span className="tb-alert" role="status">
          <ShieldAlert size={15} strokeWidth={1.9} absoluteStrokeWidth aria-hidden />
          Kill switch ON · confirmations blocked
        </span>
      ) : null}
      {killSwitch === "unreadable" ? (
        <span className="tb-alert tb-alert--muted" role="status">
          <CircleSlash size={15} strokeWidth={1.9} absoluteStrokeWidth aria-hidden />
          Kill switch unreadable
        </span>
      ) : null}

      <span className={`td-paper-banner${live ? " td-paper-banner--live" : ""}`}>
        {live ? "Live trading" : "Paper trading"}
      </span>

      <div className="profile">
        <div className="avatar" />
        <div>
          <strong>Dmitrii</strong>
          <span>Confirm mode</span>
        </div>
      </div>
    </header>
  );
}
