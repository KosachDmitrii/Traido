import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, FileText, RefreshCw, Search, XCircle } from "lucide-react";
import { formatExchangeStamp } from "@/lib/time";
import { fetchLogEvents, type ActivityEvent } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { Button, LoadingDots, SelectField, TablePager, useTablePager } from "@/ui";
import styles from "./LogsPage.module.css";

const POLL_MS = 5000;
const PAGE_LIMIT = 1000;

export function LogsPage() {
  const { t, locale } = useI18n();
  const { desk } = useDesk();
  const ru = locale === "ru";
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [knownAgents, setKnownAgents] = useState<string[]>([]);
  const [retentionDays, setRetentionDays] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [agentFilter, setAgentFilter] = useState("all");
  const [level, setLevel] = useState("all");
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    setLoading(true);
    const load = async () => {
      try {
        const data = await fetchLogEvents({ limit: PAGE_LIMIT, agent: agentFilter });
        if (!alive) return;
        setEvents(data.events);
        setKnownAgents(previous => [...new Set([...previous, ...data.events.map(e=>e.agent)])]);
        setRetentionDays(data.retention_days); setHasMore(data.has_more);
        setLoadError(false); setLoaded(true); setLastUpdate(new Date().toISOString());
      } catch {
        if (alive) setLoadError(true);
      } finally {
        if (alive) { setLoading(false); timer = setTimeout(() => void load(), POLL_MS); }
      }
    };
    void load();
    return () => { alive = false; clearTimeout(timer); };
  }, [agentFilter, revision]);

  const agents = [...new Set([...knownAgents, ...(desk?.activity.agents ?? []).map(a=>a.id), ...(agentFilter === "all" ? [] : [agentFilter])])].sort();
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter(e => (agentFilter === "all" || e.agent === agentFilter)
      && (level === "all" || (e.level || "info") === level)
      && (!q || `${e.agent} ${e.message} ${e.symbol || ""}`.toLowerCase().includes(q)));
  }, [events, query, agentFilter, level]);
  const pager = useTablePager(rows);
  const changeFilter = (set: (value:string)=>void, value:string) => {set(value); pager.setPage(1);};
  const levels = ru ? {all:"Все уровни",info:"Информация",warn:"Предупреждения",error:"Ошибки"} : {all:"All levels",info:"Info",warn:"Warnings",error:"Errors"};
  return <div className={styles.page}>
    <header className={styles.heading}><div><span className={styles.eyebrow}>TRAIDO · {ru ? "СОБЫТИЯ" : "EVENTS"}</span><h1>{ru ? "Журнал событий" : "Event log"}</h1><p>{ru ? "Работа агентов, предупреждения и ошибки приложения." : "Agent activity, warnings and application errors."}</p></div><Button variant="ghost" loading={loading} onClick={()=>setRevision(n=>n+1)}>{!loading && <RefreshCw size={14} />}{ru ? "Обновить" : "Refresh"}</Button></header>
    <div className={styles.stats}>{[
      {icon:FileText,label:ru?"Загружено событий":"Loaded events",value:events.length},
      {icon:AlertTriangle,label:levels.warn,value:events.filter(e=>e.level==="warn").length},
      {icon:XCircle,label:levels.error,value:events.filter(e=>e.level==="error").length},
    ].map(({icon:Icon,label,value})=><div key={label}><span><Icon size={16} aria-hidden />{label}</span><strong>{loaded && !loading ? value : "—"}</strong></div>)}</div>
    <section className={styles.panel}>
      <div className={styles.toolbar}><div className={styles.search}><Search size={16} aria-hidden /><input aria-label={t("logs.search.placeholder")} placeholder={t("logs.search.placeholder")} value={query} onChange={e=>changeFilter(setQuery,e.target.value)} /></div>
        <SelectField ariaLabel={t("logs.filter.all")} value={agentFilter} onChange={v=>changeFilter(setAgentFilter,v)} options={[{value:"all",label:t("logs.filter.all")},...agents.map(a=>({value:a,label:a}))]} />
        <SelectField ariaLabel={ru?"Уровень события":"Event level"} value={level} onChange={v=>changeFilter(setLevel,v)} options={Object.entries(levels).map(([value,label])=>({value,label}))} />
      </div>
      <div className={styles.meta}><span>{ru ? "Автообновление каждые 5 с" : "Refresh every 5 s"} · {lastUpdate ? `${formatExchangeStamp(lastUpdate)} ET` : "—"}</span><span>{retentionDays === null ? "—" : t("logs.retentionNote",{days:retentionDays})}</span></div>
      {loadError && <p className={styles.error} role="alert">{ru ? "Не удалось обновить события. Показаны последние полученные данные; повторяем автоматически." : "Could not refresh events. Last received data is shown; retrying automatically."}</p>}
      {loading ? <div className={styles.empty}><LoadingDots ariaLabel={ru?"Загрузка событий":"Loading events"} /></div> : !rows.length ? <div className={styles.empty}><FileText size={26} aria-hidden /><h2>{loadError && !loaded ? (ru?"Данные журнала недоступны":"Log data unavailable") : query || level!=="all" ? (ru?"Нет событий по выбранным фильтрам":"No matching events") : t("logs.empty")}</h2></div> : <div className={styles.scroll} tabIndex={0} role="region" aria-label={ru?"Таблица событий":"Events table"}><table><thead><tr><th>{ru?"Время · ET":"Time · ET"}</th><th>{ru?"Уровень":"Level"}</th><th>{ru?"Агент":"Agent"}</th><th>{ru?"Сообщение":"Message"}</th></tr></thead><tbody>{pager.slice.map((e,i)=><tr key={`${e.ts}-${e.agent}-${i}`}><td className={styles.time}>{formatExchangeStamp(e.ts)}</td><td><span className={styles.level} data-level={e.level || "info"}>{levels[(e.level || "info") as keyof typeof levels] ?? e.level}</span></td><td><span className={styles.agent}>{e.agent}</span></td><td className={styles.message}>{e.symbol && <strong className={styles.symbol}>{e.symbol}</strong>}{e.message}</td></tr>)}</tbody></table></div>}
      <footer className={styles.footer}><span>{ru?"Найдено":"Matches"}: {rows.length}</span><TablePager pager={pager} /></footer>
    </section>
    <p className={styles.scope}>{ru ? `Поиск и пагинация работают по последним ${PAGE_LIMIT} загруженным событиям выбранного агента.` : `Search and pagination cover the latest ${PAGE_LIMIT} loaded events for the selected agent.`}{hasMore ? (ru ? " В хранилище есть более ранние записи." : " Older records exist in storage.") : ""}</p>
  </div>;
}
