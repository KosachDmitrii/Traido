import { BrandLogo } from "@/layout/BrandLogo";
import { CircleSlash, Clock, ShieldAlert } from "lucide-react";
import type { CSSProperties } from "react";
import type { SessionState } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";

const DAY_MINUTES = 24 * 60;

function clockMinutes(hhmm: string | undefined): number | null {
  const parts = /^(\d{1,2}):(\d{2})$/.exec(hhmm ?? "");
  return parts ? Number(parts[1]) * 60 + Number(parts[2]) : null;
}

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

function humanGap(
  mins: number,
  t: (key: "topbar.gap.minutes" | "topbar.gap.hoursMinutes" | "topbar.gap.hours", v?: Record<string, number>) => string,
): string {
  if (mins < 60) return t("topbar.gap.minutes", { n: mins });
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest
    ? t("topbar.gap.hoursMinutes", { h: hours, m: rest })
    : t("topbar.gap.hours", { h: hours });
}

const PHASE_TONE: Record<string, string> = {
  regular: "open",
  premarket: "soon",
  after_hours: "late",
};

export function Topbar() {
  const { desk, killSwitch } = useDesk();
  const { t } = useI18n();
  const session = desk?.session;
  const { fraction, remaining } = session
    ? sessionProgress(session)
    : { fraction: 0, remaining: null };

  const gap =
    remaining !== null && remaining > 0 ? humanGap(remaining, t as never) : null;

  let copy: { title: string; detail: string } | null = null;
  if (session) {
    switch (session.phase) {
      case "regular":
        copy = {
          title: t("topbar.session.regular.title"),
          detail: gap
            ? t("topbar.session.regular.detailCountdown", { gap, time: session.closes_at })
            : t("topbar.session.regular.detail", { time: session.closes_at }),
        };
        break;
      case "premarket":
        copy = {
          title: t("topbar.session.premarket.title"),
          detail: gap
            ? t("topbar.session.premarket.detailCountdown", { gap, time: session.opens_at })
            : t("topbar.session.premarket.detail", { time: session.opens_at }),
        };
        break;
      case "after_hours":
        copy = {
          title: t("topbar.session.afterHours.title"),
          detail: t("topbar.session.afterHours.detail", { time: session.closes_at }),
        };
        break;
      case "closed_holiday":
        copy = {
          title: t("topbar.session.holiday.title"),
          detail: t("topbar.session.holiday.detail"),
        };
        break;
      case "closed_weekend":
        copy = {
          title: t("topbar.session.weekend.title"),
          detail: t("topbar.session.weekend.detail"),
        };
        break;
      default:
        copy = {
          title: t("topbar.session.unknown.title"),
          detail: t("topbar.session.unknown.detail"),
        };
    }
  }

  const tone = session ? PHASE_TONE[session.phase] : undefined;
  const live = (desk?.mode || "").toLowerCase() === "live";

  return (
    <header className="topbar">
      <div className="topbar__left">
        <BrandLogo name={t("brand.name")} />
      </div>

      <div className="topbar__center">
        <div className={`tb-session${tone ? ` tb-session--${tone}` : ""}`}>
          <span className="tb-dial" style={{ "--dial": String(fraction) } as CSSProperties}>
            <svg className="tb-dial__ring" viewBox="0 0 28 28" aria-hidden>
              <circle className="tb-dial__track" cx="14" cy="14" r="12.5" />
              <circle className="tb-dial__arc" cx="14" cy="14" r="12.5" />
            </svg>
            <Clock size={13} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
          </span>
          <span className="tb-session__now">
            <span className="tb-session__clock mono">{session?.et_time ?? "--:--"}</span>
            <span className="tb-session__zone">
              {t("topbar.session.zone", { date: session?.et_date ?? "—" })}
            </span>
          </span>
          <span className="tb-session__divider" aria-hidden />
          <span className="tb-session__text">
            <strong>{copy?.title ?? t("topbar.session.loading")}</strong>
            <span>{copy?.detail ?? ""}</span>
          </span>
        </div>
      </div>

      <div className="topbar__right">
        {killSwitch === "on" ? (
          <span className="tb-alert" role="status">
            <ShieldAlert size={15} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
            {t("topbar.kill.on")}
          </span>
        ) : null}
        {killSwitch === "unreadable" ? (
          <span className="tb-alert tb-alert--muted" role="status">
            <CircleSlash size={15} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
            {t("topbar.kill.unreadable")}
          </span>
        ) : null}

        <span className={`td-paper-banner${live ? " td-paper-banner--live" : ""}`}>
          {live ? t("topbar.mode.live") : t("topbar.mode.paper")}
        </span>
      </div>
    </header>
  );
}
