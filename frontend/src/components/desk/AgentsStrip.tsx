import { useLocation } from "react-router-dom";
import { useDesk } from "@/context/DeskContext";
import { isAgentLive } from "@/lib/api";

type Chip = { label: string; value?: string; accent?: boolean; live?: boolean };

type PageMeta = {
  title: string;
  sub: string;
  chips: Chip[];
};

function usePageMeta(): PageMeta {
  const { desk, scannerLine } = useDesk();
  const path = useLocation().pathname;
  const scanner = desk?.scanner;
  const agents = desk?.activity?.agents ?? [];
  const working = agents.filter(isAgentLive).length;
  const buys = desk?.buy_opportunities?.length ?? 0;
  const sells = desk?.sell_opportunities?.length ?? 0;
  const positions = desk?.positions?.length ?? 0;
  const orders = desk?.open_orders?.length ?? 0;
  const review = desk?.review;
  const events = desk?.activity?.events?.length ?? 0;
  const universe = (scanner?.universe || []).length;

  switch (path) {
    case "/opportunities":
      return {
        title: "Opportunities",
        sub: `Confirm queue · ${buys} buy · ${sells} sell`,
        chips: [
          { label: "Buys", value: String(buys) },
          { label: "Sells", value: String(sells) },
          { label: "Open", value: String(buys + sells), accent: buys + sells > 0 },
          { label: buys + sells ? "Awaiting you" : "Empty", accent: buys + sells === 0 },
        ],
      };
    case "/positions":
      return {
        title: "Positions",
        sub: `Broker paper · equity ${desk?.portfolio?.equity ?? "—"} · cash ${desk?.portfolio?.cash ?? "—"}`,
        chips: [
          { label: "Open", value: String(positions) },
          { label: "Orders", value: String(orders) },
          {
            label: "Day",
            value: desk?.portfolio?.day_pnl ?? "—",
          },
          { label: positions ? "In market" : "Flat", accent: true },
        ],
      };
    case "/agents":
      return {
        title: "Agents",
        sub: scannerLine,
        chips: [
          { label: "Cycle", value: String(scanner?.cycle ?? "—") },
          { label: "Univ", value: String(universe) },
          { label: "Live", value: String(working), live: working > 0 },
          { label: scanner?.running ? "Scanning" : "Idle", accent: true },
        ],
      };
    case "/journal":
      return {
        title: "Journal",
        sub: "Closed trades · Review Agent (no trading authority)",
        chips: [
          { label: "Trades", value: String(review?.trade_count ?? 0) },
          {
            label: "Win",
            value: review?.trade_count ? `${(review.win_rate * 100).toFixed(0)}%` : "—",
          },
          {
            label: "Exp",
            value: review?.expectancy != null ? `$${Number(review.expectancy).toFixed(2)}` : "—",
          },
          { label: "Read-only", accent: true },
        ],
      };
    case "/evaluation":
      return {
        title: "Evaluation",
        sub: "Walk-forward results after commission, spread and slippage",
        chips: [
          { label: "Univ", value: String(universe) },
          { label: "Bench", value: "SPY" },
          { label: "Out-of-sample", accent: true },
        ],
      };
    case "/logs": {
      const funnel = scanner?.funnel;
      return {
        title: "Logs",
        sub: funnel
          ? funnel.paused_on_full_queue
            ? `Last cycle paused on a full queue · universe ${funnel.universe_total} · ` +
              `decide or expire a proposal to resume`
            : `Last cycle · universe ${funnel.universe_total} · ` +
              `market-passed ${funnel.market_filter_passed} · ` +
              `shortlisted ${funnel.quant_shortlisted} · ` +
              `deep ${funnel.deep_analysis_started} · ` +
              `risk-passed ${funnel.risk_passed} · ${funnel.published} proposals` +
              (funnel.final_outranked ? ` · ${funnel.final_outranked} outranked` : "")
          : `Pipeline activity · ${events} buffered events`,
        chips: [
          { label: "Events", value: String(events) },
          { label: "Univ", value: String(funnel?.universe_total ?? 0) },
          { label: "Deep", value: String(funnel?.deep_analysis_started ?? 0) },
          { label: "Cycle", value: String(scanner?.cycle ?? "—") },
          { label: working ? "Live" : "Quiet", accent: true, live: working > 0 },
        ],
      };
    }
    case "/settings":
      return {
        title: "Settings",
        sub: "Paper desk · local browser + API",
        chips: [
          { label: "Univ", value: String(universe) },
          { label: "Mode", value: desk?.mode || "confirm" },
          { label: "Paper", accent: true },
        ],
      };
    default:
      return {
        title: "Traido",
        sub: scannerLine,
        chips: [{ label: "Paper", accent: true }],
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
