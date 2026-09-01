import { useCallback, useId, useState } from "react";
import { createPortal } from "react-dom";
import type { LucideIcon } from "lucide-react";
import {
  BookOpen,
  Bot,
  ChartSpline,
  ChevronRight,
  Eye,
  Gauge,
  Route,
  Rss,
  ScanLine,
  Shield,
} from "lucide-react";
import { agentDisplayStatus, type AgentState } from "@/lib/api";
import { ScanFunnelCard } from "@/components/desk/ScanFunnelCard";
import { WorkingAntsBorder } from "@/components/desk/WorkingAntsBorder";
import { useDesk } from "@/context/DeskContext";
import { formatLocalDateTime, formatLocalTime } from "@/lib/time";

type Accent = "mustard" | "taupe" | "ink" | "sage";

type AgentMeta = {
  icon: LucideIcon;
  blurb: string;
  accent: Accent;
  tip: string;
  feedsFrom: string[];
  feedsTo: string[];
};

/* Glyphs are chosen for legibility at 15px first and metaphor second: an icon
 * with eight interior strokes — a radar sweep, a wire globe, a newspaper — is a
 * smudge at this size, and eight smudges in a row read as decoration rather
 * than as a pipeline. Each one here is three or four separated strokes, and
 * where a concept already has an icon in the sidebar (the journal) the agent
 * that produces it uses the same one.
 *
 * The glyph also has to be true. An upward arrow on the technical agent claimed
 * a verdict it reaches only half the time — the same scorer subtracts for a
 * downtrend, an overbought RSI and a double top — so it is a plain price curve.
 * A wallet on the position agent named the wrong job entirely: holdings are the
 * ledger's, and this one watches for a reason to leave. */
const AGENT_META: Record<string, AgentMeta> = {
  scanner: {
    icon: ScanLine,
    blurb: "Universe pass",
    accent: "mustard",
    tip: "Walks the watchlist, builds multi-TF features, and fans work to analysts.",
    feedsFrom: [],
    feedsTo: ["technical", "news", "market"],
  },
  technical: {
    icon: ChartSpline,
    blurb: "Price & structure",
    accent: "taupe",
    tip: "Scores trend, momentum, and structure from OHLCV features.",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  news: {
    icon: Rss,
    blurb: "Headline tone",
    accent: "sage",
    tip: "Reads recent headlines and maps tone into a 0–100 news score.",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  market: {
    icon: Gauge,
    blurb: "Macro regime",
    accent: "taupe",
    tip: "Reads FRED macro series — 10y yield and unemployment — for a risk-on/risk-off posture. Not market breadth: nothing here counts advancers or decliners.",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  strategy: {
    icon: Route,
    blurb: "Trade candidate",
    accent: "mustard",
    tip: "Blends analyst scores into a BUY/SKIP candidate with entry, stop, target.",
    feedsFrom: ["technical", "news", "market"],
    feedsTo: ["risk"],
  },
  risk: {
    icon: Shield,
    blurb: "Size & gates",
    accent: "ink",
    tip: "Deterministic gates and sizing. Never places orders — only PASS/FAIL for you.",
    feedsFrom: ["strategy"],
    feedsTo: ["position"],
  },
  position: {
    icon: Eye,
    blurb: "Exit watch",
    accent: "ink",
    tip: "Watches open positions for a reason to leave — target, stop, R:R, overbought, trend break, drawdown, or a stale position. Proposes SELL; never places the order.",
    feedsFrom: ["risk"],
    feedsTo: ["review"],
  },
  review: {
    icon: BookOpen,
    blurb: "Closed-trade journal",
    accent: "sage",
    tip: "Post-trade analytics only — win rate, expectancy, notes. No trading authority.",
    feedsFrom: ["position"],
    feedsTo: [],
  },
};

const LANES: { id: string; label: string; agentIds: string[] }[] = [
  { id: "scan", label: "Scan", agentIds: ["scanner"] },
  { id: "analyze", label: "Analyze", agentIds: ["technical", "news", "market"] },
  { id: "decide", label: "Decide", agentIds: ["strategy", "risk"] },
  { id: "book", label: "Book", agentIds: ["position", "review"] },
];

function statusLabel(status: string) {
  if (status === "working") return "Working";
  if (status === "done") return "Done";
  if (status === "error") return "Error";
  return "Idle";
}

function formatScore(agent: AgentState) {
  if (agent.score === null || agent.score === undefined) return "—";
  if (agent.id === "risk") return agent.score >= 100 ? "OK" : "X";
  return String(Math.round(Number(agent.score)));
}

function scoreHint(agent: AgentState) {
  if (agent.id === "risk") {
    if (agent.score == null) return "No risk verdict yet";
    return agent.score >= 100 ? "PASS — size within gates" : "FAIL — blocked by risk";
  }
  if (agent.score == null) return "No score this cycle";
  return `Score ${Math.round(Number(agent.score))} / 100`;
}

function AgentNode({
  agent,
  byId,
}: {
  agent: AgentState;
  byId: Record<string, AgentState | undefined>;
}) {
  const tipId = useId();
  const [tipPos, setTipPos] = useState<{ left: number; top: number; place: "above" | "below" } | null>(
    null,
  );
  const meta = AGENT_META[agent.id] ?? {
    icon: Bot,
    blurb: "Pipeline agent",
    accent: "taupe" as const,
    tip: "Part of the Traido confirm pipeline.",
    feedsFrom: [] as string[],
    feedsTo: [] as string[],
  };
  const Icon = meta.icon;
  const status = agentDisplayStatus(agent);
  const detail = agent.detail || agent.last_symbol || "Waiting for next pass";
  const fromNames = meta.feedsFrom.map((id) => byId[id]?.name || id);
  const toNames = meta.feedsTo.map((id) => byId[id]?.name || id);

  const showTip = useCallback((el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const tipH = 220;
    const spaceBelow = window.innerHeight - r.bottom;
    const place: "above" | "below" = spaceBelow < tipH && r.top > tipH ? "above" : "below";
    const left = Math.min(window.innerWidth - 16, Math.max(16, r.left + r.width / 2));
    const top = place === "below" ? r.bottom + 10 : r.top - 10;
    setTipPos({ left, top, place });
  }, []);

  const tip = tipPos ? (
    <div
      id={tipId}
      role="tooltip"
      className={`ag-tip ag-tip--fixed ag-tip--${tipPos.place}`}
      style={{ left: tipPos.left, top: tipPos.top }}
    >
      <strong>{agent.name}</strong>
      <p>{meta.tip}</p>
      <dl>
        <div>
          <dt>Status</dt>
          <dd>{statusLabel(status)}</dd>
        </div>
        <div>
          <dt>Score</dt>
          <dd>{scoreHint(agent)}</dd>
        </div>
        <div>
          <dt>Symbol</dt>
          <dd className="mono">{agent.last_symbol || "—"}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{formatLocalDateTime(agent.updated_at)}</dd>
        </div>
        {fromNames.length ? (
          <div>
            <dt>From</dt>
            <dd>{fromNames.join(" · ")}</dd>
          </div>
        ) : null}
        {toNames.length ? (
          <div>
            <dt>To</dt>
            <dd>{toNames.join(" · ")}</dd>
          </div>
        ) : null}
      </dl>
      <p className="ag-tip__live">{detail}</p>
    </div>
  ) : null;

  return (
    <button
      type="button"
      className={`ag-node ag-node--${status} ag-node--${meta.accent}`}
      aria-label={`${agent.name}, ${statusLabel(status)}`}
      aria-describedby={tipPos ? tipId : undefined}
      onMouseEnter={(e) => showTip(e.currentTarget)}
      onFocus={(e) => showTip(e.currentTarget)}
      onMouseLeave={() => setTipPos(null)}
      onBlur={() => setTipPos(null)}
    >
      <span className={`ag-node__icon ag-node__icon--${meta.accent}`}>
        <Icon size={16} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
      </span>
      <span className="ag-node__body">
        <span className="ag-node__name">{agent.name}</span>
        <span className="ag-node__meta">{meta.blurb}</span>
      </span>
      <span className={`ag-node__score mono ag-node__score--${status}`}>
        {formatScore(agent)}
      </span>
      {status === "working" ? <WorkingAntsBorder radius={12} /> : null}
      {tip && createPortal(tip, document.body)}
    </button>
  );
}

export function AgentsPage() {
  const { desk } = useDesk();
  const agents = desk?.activity?.agents ?? [];
  const byId = Object.fromEntries(agents.map((a) => [a.id, a]));
  const ordered = LANES.flatMap((lane) =>
    lane.agentIds.map((id) => byId[id]).filter((a): a is AgentState => Boolean(a)),
  );

  return (
    <div className="ag-page">
      <section className="ag-rail" aria-label="Pipeline">
        {LANES.map((lane, i) => {
          const laneAgents = lane.agentIds
            .map((id) => byId[id])
            .filter((a): a is AgentState => Boolean(a));
          const hot = laneAgents.some((a) => a.status === "working" || a.status === "done");
          return (
            <div className="ag-rail__row" key={lane.id}>
              <div className={`ag-lane${hot ? " ag-lane--hot" : ""}`}>
                <div className="ag-lane__head">{lane.label}</div>
                <div className="ag-lane__nodes">
                  {laneAgents.map((a) => (
                    <AgentNode key={a.id} agent={a} byId={byId} />
                  ))}
                </div>
              </div>
              {i < LANES.length - 1 ? (
                <div className="ag-rail__join" aria-hidden>
                  <ChevronRight size={18} strokeWidth={1.75} />
                </div>
              ) : null}
            </div>
          );
        })}
      </section>

      <ScanFunnelCard />

      <section className="ag-sheet card">
        <div className="card-head">
          <div>
            <h2>Status board</h2>
            <div className="sub">Compact readout · hover nodes above for wiring</div>
          </div>
        </div>
        <div className="ag-sheet__rows">
          {ordered.map((a) => {
            const meta = AGENT_META[a.id];
            const Icon = meta?.icon ?? Bot;
            const status = agentDisplayStatus(a);
            return (
              <div className={`ag-row ag-row--${status}`} key={a.id}>
                <span className={`ag-row__ico ag-row__ico--${meta?.accent || "taupe"}`}>
                  <Icon size={15} strokeWidth={1.75} absoluteStrokeWidth aria-hidden />
                </span>
                <strong>{a.name}</strong>
                <span className={`ag-row__pill ag-row__pill--${status}`}>{statusLabel(status)}</span>
                <span className="ag-row__score mono">{formatScore(a)}</span>
                <span className="ag-row__detail">{a.detail || a.last_symbol || "—"}</span>
                <span className="ag-row__time mono">{formatLocalTime(a.updated_at)}</span>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
