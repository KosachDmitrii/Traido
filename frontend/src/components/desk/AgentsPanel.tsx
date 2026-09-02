import { Link } from "react-router-dom";
import { agentDisplayStatus, type AgentState, type DeskResponse } from "@/lib/api";
import { WorkingAntsBorder } from "@/components/desk/WorkingAntsBorder";
import { useT } from "@/i18n/I18nProvider";
import { formatExchangeTime } from "@/lib/time";

function statusLabel(
  status: string,
  t: ReturnType<typeof useT>,
): string {
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

export function AgentsPanel({ desk }: { desk: DeskResponse | null }) {
  const t = useT();
  const agents = desk?.activity?.agents ?? [];
  const events = [...(desk?.activity?.events ?? [])].reverse().slice(0, 18);
  // The raw status names the stage the pass is on this instant, which is the
  // more useful headline. Fall back to any live agent so the subtitle cannot say
  // "last pass" while the rows below it are animating.
  const working =
    agents.find((a) => a.status === "working") ??
    agents.find((a) => agentDisplayStatus(a) === "working");

  return (
    <section className="grid-2">
      <div className="card">
        <div className="card-head">
          <div>
            <h2>{t("desk.agents.title")}</h2>
            <div className="sub">
              {working
                ? `${working.name} · ${working.detail || working.last_symbol || "…"}`
                : t("desk.agents.subIdle")}
            </div>
          </div>
          <Link className="sub" to="/agents" style={{ color: "inherit" }}>
            {t("desk.agents.link")}
          </Link>
        </div>
        <div className="agent-list">
          {agents.map((a) => {
            const status = agentDisplayStatus(a);
            const live = status === "working";
            return (
              <div className={`agent${live ? " agent--working" : ""}`} key={a.id}>
                {live ? <WorkingAntsBorder radius={12} /> : null}
                <span className={`dot ${status}`} />
                <div className="meta">
                  <strong>{a.name}</strong>
                  <span>
                    {statusLabel(status, t)} · {a.detail || a.last_symbol || "—"}
                  </span>
                </div>
                <div className="score">{formatScore(a, t)}</div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>{t("desk.activity.title")}</h2>
            <div className="sub">{t("desk.activity.sub")}</div>
          </div>
          <Link className="sub" to="/logs" style={{ color: "inherit" }}>
            {t("desk.activity.link")}
          </Link>
        </div>
        <div className="activity-feed">
          {events.length === 0 ? (
            <div className="row">
              <span />
              <span className="ag">—</span>
              <span>{t("desk.activity.empty")}</span>
            </div>
          ) : (
            events.map((e, i) => {
              const time = formatExchangeTime(e.ts);
              const lvl = e.level === "warn" || e.level === "error" ? e.level : "";
              const msg = e.symbol ? `${e.symbol}: ${e.message}` : e.message;
              return (
                <div className={`row ${lvl}`} key={`${e.ts}-${i}`} title={msg}>
                  <span>{time}</span>
                  <span className="ag">{e.agent}</span>
                  <span className="msg">{msg}</span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}
