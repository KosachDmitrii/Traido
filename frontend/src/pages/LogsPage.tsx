import { useMemo, useState } from "react";
import { formatLocalTime } from "@/lib/time";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";

export function LogsPage() {
  const t = useT();
  const { desk } = useDesk();
  const events = desk?.activity?.events ?? [];
  const [agentFilter, setAgentFilter] = useState("all");
  const [query, setQuery] = useState("");

  const agents = useMemo(() => {
    const ids = new Set(events.map((e) => e.agent));
    return ["all", ...Array.from(ids).sort()];
  }, [events]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return [...events]
      .reverse()
      .filter((e) => (agentFilter === "all" ? true : e.agent === agentFilter))
      .filter((e) => {
        if (!q) return true;
        const hay = `${e.agent} ${e.message} ${e.symbol || ""}`.toLowerCase();
        return hay.includes(q);
      });
  }, [events, agentFilter, query]);

  return (
    <section className="card page-card">
      <div className="logs-toolbar">
        <input
          className="logs-search"
          placeholder={t("logs.search.placeholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="logs-select"
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
        >
          {agents.map((a) => (
            <option key={a} value={a}>
              {a === "all" ? t("logs.filter.all") : a}
            </option>
          ))}
        </select>
      </div>

      <div className="activity-feed logs-feed">
        {rows.length === 0 ? (
          <div className="row">
            <span />
            <span className="ag">—</span>
            <span>{t("logs.empty")}</span>
          </div>
        ) : (
          rows.map((e, i) => {
            const lvl = e.level === "warn" || e.level === "error" ? e.level : "";
            const msg = e.symbol ? `${e.symbol}: ${e.message}` : e.message;
            return (
              <div className={`row ${lvl}`} key={`${e.ts}-${e.agent}-${i}`} title={e.ts}>
                <span>{formatLocalTime(e.ts)}</span>
                <span className="ag">{e.agent}</span>
                <span>{msg}</span>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
