import type { DeskResponse } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";
import { watchApproaching, watchInOrNearZone } from "@/lib/watchGeometry";

/**
 * Session meaning without inventing BUY cards.
 *
 * Empty RTH with only WAIT plans used to feel like a dead product. This strip
 * states the desk's actual decisions: confirmable BUYs, entry plans, near-zone
 * watches, and what the last scan looked at.
 */

export function SessionDecisionStrip({ desk }: { desk: DeskResponse | null }) {
  const t = useT();
  if (!desk) return null;

  const buys = desk.buy_opportunities?.length ?? 0;
  const sells = desk.sell_opportunities?.length ?? 0;
  const plans = desk.entry_watches ?? [];
  const inZone = plans.filter(watchInOrNearZone).length;
  const near = plans.filter(watchApproaching).length;
  const funnel = desk.scanner?.funnel;
  const watchFunnel = desk.watch_funnel;
  const deep = funnel?.deep_analysis_started ?? null;
  const published = funnel?.published ?? null;

  return (
    <section className="session-strip" aria-label={t("session.strip.label")}>
      <span className="session-strip__item">
        <span className="session-strip__k">{t("session.strip.buy")}</span>
        <b className="mono">{buys}</b>
      </span>
      <span className="session-strip__item">
        <span className="session-strip__k">{t("session.strip.plans")}</span>
        <b className="mono">{plans.length}</b>
      </span>
      {watchFunnel?.triggered != null && watchFunnel.triggered > 0 ? (
        <span className="session-strip__item">
          <span className="session-strip__k">{t("session.strip.triggered")}</span>
          <b className="mono">{watchFunnel.triggered}</b>
        </span>
      ) : null}
      {watchFunnel?.admitted != null && watchFunnel.admitted > 0 ? (
        <span className="session-strip__item">
          <span className="session-strip__k">{t("session.strip.admitted")}</span>
          <b className="mono">{watchFunnel.admitted}</b>
        </span>
      ) : null}
      <span className="session-strip__item">
        <span className="session-strip__k">{t("session.strip.inZone")}</span>
        <b className="mono">{inZone}</b>
      </span>
      <span className="session-strip__item">
        <span className="session-strip__k">{t("session.strip.near")}</span>
        <b className="mono">{near}</b>
      </span>
      <span className="session-strip__item">
        <span className="session-strip__k">{t("session.strip.sell")}</span>
        <b className="mono">{sells}</b>
      </span>
      {deep != null ? (
        <span className="session-strip__item session-strip__item--muted">
          <span className="session-strip__k">{t("session.strip.scanned")}</span>
          <b className="mono">
            {deep}
            {published != null ? ` → ${published}` : ""}
          </b>
        </span>
      ) : null}
      {!buys && plans.length > 0 ? (
        <span className="session-strip__note">{t("session.strip.waitingNote")}</span>
      ) : null}
      {!buys && !plans.length && !sells ? (
        <span className="session-strip__note">{t("session.strip.emptyNote")}</span>
      ) : null}
    </section>
  );
}
