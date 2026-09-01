import { useEffect, useMemo, useState } from "react";
import { formatExchangeStamp } from "@/lib/time";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import { SelectField, TablePager, useTablePager } from "@/ui";

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

  const agentOptions = useMemo(
    () =>
      agents.map((a) => ({
        value: a,
        label: a === "all" ? t("logs.filter.all") : a,
      })),
    [agents, t],
  );

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

  const pager = useTablePager(rows);
  const { setPage } = pager;

  useEffect(() => {
    setPage(1);
  }, [agentFilter, query, setPage]);

  return (
    <section className="card page-card">
      <div className="logs-toolbar">
        <input
          className="logs-search"
          placeholder={t("logs.search.placeholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <SelectField
          className="logs-agent-select"
          ariaLabel={t("logs.filter.all")}
          value={agentFilter}
          onChange={setAgentFilter}
          options={agentOptions}
        />
      </div>

      <div className="activity-feed logs-feed">
        {rows.length === 0 ? (
          <div className="row">
            <span />
            <span className="ag">—</span>
            <span>{t("logs.empty")}</span>
          </div>
        ) : (
          pager.slice.map((e, i) => {
            const lvl = e.level === "warn" || e.level === "error" ? e.level : "";
            const msg = e.symbol ? `${e.symbol}: ${e.message}` : e.message;
            return (
              <div className={`row ${lvl}`} key={`${e.ts}-${e.agent}-${i}`} title={`${formatExchangeStamp(e.ts)} ET`}>
                <span className="mono">{formatExchangeStamp(e.ts)}</span>
                <span className="ag">{e.agent}</span>
                <span>{msg}</span>
              </div>
            );
          })
        )}
      </div>
      <TablePager pager={pager} />
    </section>
  );
}
