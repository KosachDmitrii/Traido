import { Link } from "react-router-dom";
import { agentDisplayStatus, type AgentState, type DeskResponse } from "@/lib/api";
import { WorkingAntsBorder } from "@/components/desk/WorkingAntsBorder";
import { formatLocalTime } from "@/lib/time";

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

export function AgentsPanel({ desk }: { desk: DeskResponse | null }) {
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
            <h2>AI Agents</h2>
            <div className="sub">
              {working
                ? `${working.name} · ${working.detail || working.last_symbol || "…"}`
                : "Pipeline status · last pass"}
            </div>
          </div>
          <Link className="sub" to="/agents" style={{ color: "inherit" }}>
            Pipeline →
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
                    {statusLabel(status)} · {a.detail || a.last_symbol || "—"}
                  </span>
                </div>
                <div className="score">{formatScore(a)}</div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h2>Activity</h2>
            <div className="sub">Live feed from the scan pipeline</div>
          </div>
          <Link className="sub" to="/logs" style={{ color: "inherit" }}>
            All logs →
          </Link>
        </div>
        <div className="activity-feed">
          {events.length === 0 ? (
            <div className="row">
              <span />
              <span className="ag">—</span>
              <span>Waiting for first scan…</span>
            </div>
          ) : (
            events.map((e, i) => {
              const t = formatLocalTime(e.ts);
              const lvl = e.level === "warn" || e.level === "error" ? e.level : "";
              const msg = e.symbol ? `${e.symbol}: ${e.message}` : e.message;
              return (
                <div className={`row ${lvl}`} key={`${e.ts}-${i}`} title={e.ts || undefined}>
                  <span>{t}</span>
                  <span className="ag">{e.agent}</span>
                  <span>{msg}</span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}
