import { useLocation } from "react-router-dom";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import { isAgentLive } from "@/lib/api";

type Chip = { label: string; value?: string; accent?: boolean; live?: boolean };

type PageMeta = {
  title: string;
  sub: string;
  chips: Chip[];
};

function usePageMeta(): PageMeta {
  const t = useT();
  const { desk, scannerLine } = useDesk();
  const path = useLocation().pathname;
  const scanner = desk?.scanner;
  const agents = desk?.activity?.agents ?? [];
  const working = agents.filter(isAgentLive).length;
  const buys = desk?.buy_opportunities?.length ?? 0;
  const waits = desk?.entry_watches?.length ?? 0;
  const sells = desk?.sell_opportunities?.length ?? 0;
  const positions = desk?.positions?.length ?? 0;
  const orders = desk?.open_orders?.length ?? 0;
  const review = desk?.review;
  const events = desk?.activity?.events?.length ?? 0;
  const universe = (scanner?.universe || []).length;

  switch (path) {
    case "/opportunities":
      return {
        title: t("strip.opportunities.title"),
        sub: t("strip.opportunities.sub", { buys, sells }),
        chips: [
          { label: t("strip.opportunities.buys"), value: String(buys) },
          { label: t("strip.opportunities.sells"), value: String(sells) },
          { label: t("strip.opportunities.open"), value: String(buys + sells), accent: buys + sells > 0 },
          {
            label: buys + sells ? t("strip.opportunities.awaiting") : t("strip.opportunities.empty"),
            accent: buys + sells === 0,
          },
        ],
      };
    case "/positions":
      return {
        title: t("strip.positions.title"),
        sub: t("strip.positions.sub", {
          equity: desk?.portfolio?.equity ?? "—",
          cash: desk?.portfolio?.cash ?? "—",
        }),
        chips: [
          { label: t("strip.positions.open"), value: String(positions) },
          { label: t("strip.positions.orders"), value: String(orders) },
          {
            label: t("strip.positions.day"),
            value: desk?.portfolio?.day_pnl ?? "—",
          },
          {
            label: positions ? t("strip.positions.inMarket") : t("strip.positions.flat"),
            accent: true,
          },
        ],
      };
    case "/agents":
      return {
        title: t("strip.agents.title"),
        sub: scannerLine,
        chips: [
          { label: t("strip.agents.cycle"), value: String(scanner?.cycle ?? "—") },
          { label: t("strip.agents.univ"), value: String(universe) },
          { label: t("strip.agents.live"), value: String(working), live: working > 0 },
          {
            label: scanner?.running ? t("strip.agents.scanning") : t("strip.agents.idle"),
            accent: true,
          },
        ],
      };
    case "/journal":
      return {
        title: t("strip.journal.title"),
        sub: t("strip.journal.sub"),
        chips: [
          { label: t("strip.journal.trades"), value: String(review?.trade_count ?? 0) },
          {
            label: t("strip.journal.win"),
            value: review?.trade_count ? `${(review.win_rate * 100).toFixed(0)}%` : "—",
          },
          {
            label: t("strip.journal.exp"),
            value: review?.expectancy != null ? `$${Number(review.expectancy).toFixed(2)}` : "—",
          },
          { label: t("strip.journal.readonly"), accent: true },
        ],
      };
    case "/evaluation":
      return {
        title: t("strip.evaluation.title"),
        sub: t("strip.evaluation.sub"),
        chips: [
          { label: t("strip.evaluation.univ"), value: String(universe) },
          { label: t("strip.evaluation.bench"), value: "SPY" },
          { label: t("strip.evaluation.oos"), accent: true },
        ],
      };
    case "/strategies":
      return {
        title: t("strip.strategies.title"),
        sub: t("strip.strategies.sub"),
        chips: [
          { label: t("strip.strategies.versions"), accent: true },
          { label: t("strip.strategies.production") },
        ],
      };
    case "/logs": {
      const funnel = scanner?.funnel;
      return {
        title: t("strip.logs.title"),
        sub: funnel
          ? funnel.paused_on_full_queue
            ? t("strip.logs.sub.fullQueue", { univ: funnel.universe_total })
            : t("strip.logs.sub.cycle", {
                univ: funnel.universe_total,
                market: funnel.market_filter_passed,
                short: funnel.quant_shortlisted,
                deep: funnel.deep_analysis_started,
                risk: funnel.risk_passed,
                published: funnel.published,
              }) +
              (funnel.final_outranked
                ? t("strip.logs.sub.outranked", { n: funnel.final_outranked })
                : "")
          : t("strip.logs.sub.events", { n: events }),
        chips: [
          { label: t("strip.logs.events"), value: String(events) },
          { label: t("strip.logs.univ"), value: String(funnel?.universe_total ?? 0) },
          { label: t("strip.logs.deep"), value: String(funnel?.deep_analysis_started ?? 0) },
          { label: t("strip.logs.cycle"), value: String(scanner?.cycle ?? "—") },
          {
            label: working ? t("strip.logs.live") : t("strip.logs.quiet"),
            accent: true,
            live: working > 0,
          },
        ],
      };
    }
    case "/settings":
      return {
        title: t("strip.settings.title"),
        sub: t("strip.settings.sub"),
        chips: [
          { label: t("strip.settings.univ"), value: String(universe) },
          { label: t("strip.settings.mode"), value: desk?.mode || "confirm" },
          { label: t("strip.settings.paper"), accent: true },
        ],
      };
    default:
      return {
        title: t("strip.default.title"),
        sub: t("strip.default.sub", { waits, buys, sells }),
        chips: [{ label: t("strip.default.paper"), accent: true }],
      };
  }
}

/** Shared dark status strip — same chrome, page-specific title & chips. */
export function PageStrip() {
  const meta = usePageMeta();

  return (
    <header className="ag-bar">
      <div className="ag-bar__title">
        <h2>{meta.title}</h2>
        <span className="ag-bar__sub">{meta.sub}</span>
      </div>
      <div className="ag-bar__chips">
        {meta.chips.map((c) => (
          <span
            key={`${c.label}-${c.value ?? ""}`}
            className={`ag-chip${c.accent ? " ag-chip--accent" : ""}${c.live ? " ag-chip--live" : ""}`}
          >
            {c.label}
            {c.value != null ? (
              <>
                {" "}
                <b className="mono">{c.value}</b>
              </>
            ) : null}
          </span>
        ))}
      </div>
    </header>
  );
}
