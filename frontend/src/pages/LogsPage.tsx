import { useCallback, useEffect, useMemo, useState } from "react";
import { formatExchangeStamp } from "@/lib/time";
import { fetchLogEvents, type ActivityEvent } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";
import { SelectField, TablePager, useTablePager } from "@/ui";

const POLL_MS = 5000;
const PAGE_LIMIT = 1000;

export function LogsPage() {
  const t = useT();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [retentionDays, setRetentionDays] = useState(30);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [agentFilter, setAgentFilter] = useState("all");
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await fetchLogEvents({ limit: PAGE_LIMIT, agent: agentFilter });
      setEvents(data.events);
      setRetentionDays(data.retention_days);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "logs_failed");
    }
  }, [agentFilter]);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

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
    return events.filter((e) => {
      if (!q) return true;
      const hay = `${e.agent} ${e.message} ${e.symbol || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [events, query]);

  const pager = useTablePager(rows);
  const { setPage } = pager;

  useEffect(() => {
    setPage(1);
  }, [agentFilter, query, setPage]);

  return (
    <section className="card page-card">
      <p className="logs-retention-note">
        {t("logs.retentionNote", { days: retentionDays })}
      </p>
      {loadError ? <p className="logs-error">{loadError}</p> : null}
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
              <div className={`row ${lvl}`} key={`${e.ts}-${e.agent}-${i}`} title={`${formatExchangeStamp(e.ts)} ET · ${msg}`}>
                <span className="mono">{formatExchangeStamp(e.ts)}</span>
                <span className="ag">{e.agent}</span>
                <span className="msg">{msg}</span>
              </div>
            );
          })
        )}
      </div>
      <TablePager pager={pager} />
    </section>
  );
}
