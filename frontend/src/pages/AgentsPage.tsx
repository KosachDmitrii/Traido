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
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";
import { formatLocalDateTime, formatLocalTime } from "@/lib/time";

type Accent = "mustard" | "taupe" | "ink" | "sage";

type AgentMeta = {
  icon: LucideIcon;
  blurbKey: MessageKey;
  accent: Accent;
  tipKey: MessageKey;
  feedsFrom: string[];
  feedsTo: string[];
};

const AGENT_META: Record<string, AgentMeta> = {
  scanner: {
    icon: ScanLine,
    blurbKey: "agents.meta.scanner.blurb",
    accent: "mustard",
    tipKey: "agents.meta.scanner.tip",
    feedsFrom: [],
    feedsTo: ["technical", "news", "market"],
  },
  technical: {
    icon: ChartSpline,
    blurbKey: "agents.meta.technical.blurb",
    accent: "taupe",
    tipKey: "agents.meta.technical.tip",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  news: {
    icon: Rss,
    blurbKey: "agents.meta.news.blurb",
    accent: "sage",
    tipKey: "agents.meta.news.tip",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  market: {
    icon: Gauge,
    blurbKey: "agents.meta.market.blurb",
    accent: "taupe",
    tipKey: "agents.meta.market.tip",
    feedsFrom: ["scanner"],
    feedsTo: ["strategy"],
  },
  strategy: {
    icon: Route,
    blurbKey: "agents.meta.strategy.blurb",
    accent: "mustard",
    tipKey: "agents.meta.strategy.tip",
    feedsFrom: ["technical", "news", "market"],
    feedsTo: ["risk"],
  },
  risk: {
    icon: Shield,
    blurbKey: "agents.meta.fallback.blurb",
    accent: "ink",
    tipKey: "agents.meta.fallback.tip",
    feedsFrom: ["strategy"],
    feedsTo: ["position"],
  },
  position: {
    icon: Eye,
    blurbKey: "agents.meta.position.blurb",
    accent: "ink",
    tipKey: "agents.meta.position.tip",
    feedsFrom: ["risk"],
    feedsTo: ["review"],
  },
  review: {
    icon: BookOpen,
    blurbKey: "agents.meta.review.blurb",
    accent: "sage",
    tipKey: "agents.meta.review.tip",
    feedsFrom: ["position"],
    feedsTo: [],
  },
};

const LANES: { id: string; labelKey: MessageKey; agentIds: string[] }[] = [
  { id: "scan", labelKey: "agents.lane.scan", agentIds: ["scanner"] },
  { id: "analyze", labelKey: "agents.lane.analyze", agentIds: ["technical", "news", "market"] },
  { id: "decide", labelKey: "agents.lane.decide", agentIds: ["strategy", "risk"] },
  { id: "book", labelKey: "agents.lane.book", agentIds: ["position", "review"] },
];

function statusLabel(status: string, t: ReturnType<typeof useT>) {
  if (status === "working") return t("agents.status.working");
  if (status === "done") return t("agents.status.done");
  if (status === "error") return t("agents.status.error");
  return t("agents.status.idle");
}

function formatScore(agent: AgentState, t: ReturnType<typeof useT>) {
  if (agent.score === null || agent.score === undefined) return "—";
  if (agent.id === "risk") return agent.score >= 100 ? t("agents.score.pass") : t("agents.score.fail");
  return String(Math.round(Number(agent.score)));
}

function scoreHint(agent: AgentState, t: ReturnType<typeof useT>) {
  if (agent.id === "risk") {
    if (agent.score == null) return t("agents.score.risk.none");
    return agent.score >= 100 ? t("agents.score.risk.pass") : t("agents.score.risk.fail");
  }
  if (agent.score == null) return t("agents.score.none");
  return t("agents.score.value", { n: Math.round(Number(agent.score)) });
}

function AgentNode({
  agent,
  byId,
  t,
}: {
  agent: AgentState;
  byId: Record<string, AgentState | undefined>;
  t: ReturnType<typeof useT>;
}) {
  const tipId = useId();
  const [tipPos, setTipPos] = useState<{ left: number; top: number; place: "above" | "below" } | null>(
    null,
  );
  const meta = AGENT_META[agent.id] ?? {
    icon: Bot,
    blurbKey: "agents.meta.fallback.blurb" as MessageKey,
    accent: "taupe" as const,
    tipKey: "agents.meta.fallback.tip" as MessageKey,
    feedsFrom: [] as string[],
    feedsTo: [] as string[],
  };
  const Icon = meta.icon;
  const status = agentDisplayStatus(agent);
  const detail = agent.detail || agent.last_symbol || t("agents.waiting");
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
      <p>{t(meta.tipKey)}</p>
      <dl>
        <div>
          <dt>{t("agents.tip.status")}</dt>
          <dd>{statusLabel(status, t)}</dd>
        </div>
        <div>
          <dt>{t("agents.tip.score")}</dt>
          <dd>{scoreHint(agent, t)}</dd>
        </div>
        <div>
          <dt>{t("agents.tip.symbol")}</dt>
          <dd className="mono">{agent.last_symbol || "—"}</dd>
        </div>
        <div>
          <dt>{t("agents.tip.updated")}</dt>
          <dd>{formatLocalDateTime(agent.updated_at)}</dd>
        </div>
        {fromNames.length ? (
          <div>
            <dt>{t("agents.tip.from")}</dt>
            <dd>{fromNames.join(" · ")}</dd>
          </div>
        ) : null}
        {toNames.length ? (
          <div>
            <dt>{t("agents.tip.to")}</dt>
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
      aria-label={t("agents.node.aria", { name: agent.name, status: statusLabel(status, t) })}
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
        <span className="ag-node__meta">{t(meta.blurbKey)}</span>
      </span>
      <span className={`ag-node__score mono ag-node__score--${status}`}>
        {formatScore(agent, t)}
      </span>
      {status === "working" ? <WorkingAntsBorder radius={12} /> : null}
      {tip && createPortal(tip, document.body)}
    </button>
  );
}

export function AgentsPage() {
  const t = useT();
  const { desk } = useDesk();
  const agents = desk?.activity?.agents ?? [];
  const byId = Object.fromEntries(agents.map((a) => [a.id, a]));
  const ordered = LANES.flatMap((lane) =>
    lane.agentIds.map((id) => byId[id]).filter((a): a is AgentState => Boolean(a)),
  );

  return (
    <div className="ag-page">
      <section className="ag-rail" aria-label={t("agents.pipeline.aria")}>
        {LANES.map((lane, i) => {
          const laneAgents = lane.agentIds
            .map((id) => byId[id])
            .filter((a): a is AgentState => Boolean(a));
          const hot = laneAgents.some((a) => a.status === "working" || a.status === "done");
          return (
            <div className="ag-rail__row" key={lane.id}>
              <div className={`ag-lane${hot ? " ag-lane--hot" : ""}`}>
                <div className="ag-lane__head">{t(lane.labelKey)}</div>
                <div className="ag-lane__nodes">
                  {laneAgents.map((a) => (
                    <AgentNode key={a.id} agent={a} byId={byId} t={t} />
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
            <h2>{t("agents.board.title")}</h2>
            <div className="sub">{t("agents.board.sub")}</div>
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
                <span className={`ag-row__pill ag-row__pill--${status}`}>{statusLabel(status, t)}</span>
                <span className="ag-row__score mono">{formatScore(a, t)}</span>
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
