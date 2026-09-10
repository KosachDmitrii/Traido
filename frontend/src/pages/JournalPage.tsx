import { useEffect, useState } from "react";
import { BookOpen, CheckCheck, TrendingUp, ReceiptText } from "lucide-react";
import { fetchJournalPage, fetchReview, type JournalPageResponse, type ReviewPayload } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { TablePager, Button } from "@/ui";
import styles from "./JournalPage.module.css";

function numeric(value: unknown) { return value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value); }
function price(value: unknown, signed = false) {
  const n = numeric(value);
  return n === null ? "—" : `${signed && n > 0 ? "+" : ""}${n.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

export function JournalPage() {
  const { t, locale } = useI18n();
  const ru = locale === "ru";
  const { desk } = useDesk();
  const [review, setReview] = useState<ReviewPayload | null>(null);
  const [data, setData] = useState<JournalPageResponse | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let alive = true;
    fetchReview().then(r => { if (alive) setReview(r); }).catch(() => undefined);
    return () => { alive = false; };
  }, [desk?.review?.trade_count]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false);
    fetchJournalPage(page, pageSize, controller.signal).then(next => {
      if (!controller.signal.aborted) { setData(next); setPage(next.page); }
    }).catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [page, pageSize, retry, desk?.review?.trade_count]);
  const r = review ?? desk?.review;
  const pager = {
    page, pageSize, pageCount: data?.page_count ?? 1, total: data?.total ?? 0, slice: data?.items ?? [], showPager: !!data?.total,
    canPrev: !loading && page > 1, canNext: !loading && page < (data?.page_count ?? 1),
    setPage: (next: number) => setPage(Math.max(1, Math.min(next, data?.page_count ?? 1))),
    setPageSize: (size: number) => { setPageSize(size); setPage(1); },
  };
  const stats = [
    { icon: BookOpen, label: ru ? "Сделок в журнале" : "Journal trades", value: data?.total ?? "—" },
    { icon: CheckCheck, label: ru ? "Прибыльных сделок*" : "Winning trades*", value: r?.trade_count ? `${(r.win_rate * 100).toFixed(1)}%` : "—" },
    { icon: TrendingUp, label: ru ? "Средний результат* · USD" : "Average result* · USD", value: price(r?.expectancy, true) },
  ];
  return <div className={styles.page}>
    <header className={styles.heading}><div><span className={styles.eyebrow}>ALPACA PAPER</span><h1>{ru ? "Торговый журнал" : "Trade journal"}</h1><p>{ru ? "История закрытых сделок и их результаты." : "Closed trades and their outcomes."}</p></div><span className={styles.badge}><ReceiptText size={15} />{ru ? "Закрытые сделки" : "Closed trades"}</span></header>
    <div><div className={styles.stats}>{stats.map(({ icon: Icon, label, value }) => <div key={label}><span><Icon size={16} aria-hidden />{label}</span><strong>{value}</strong></div>)}</div><p className={styles.scope}>{ru ? `* Аналитика по последним ${r?.trade_count ?? "—"} сделкам, максимум 200. Таблица содержит всю историю.` : `* Analytics cover the latest ${r?.trade_count ?? "—"} trades, up to 200. The table contains the full history.`}</p></div>
    <section className={styles.history} aria-busy={loading}>
      <div className={styles.tableHead}><h2>{ru ? "История сделок" : "Trade history"}</h2><span>{ru ? "Сначала последние · USD" : "Newest first · USD"}</span></div>
      {loading || error || !data?.items.length ? <div className={styles.empty} role="status"><BookOpen size={28} aria-hidden /><h3>{loading ? (ru ? "Загружаем сделки…" : "Loading trades…") : error ? (ru ? "Не удалось загрузить журнал" : "Could not load the journal") : t("journal.empty")}</h3>{error && <Button variant="ghost" onClick={() => setRetry(n => n + 1)}>{ru ? "Повторить" : "Retry"}</Button>}</div> : <div className={styles.scroll} tabIndex={0} role="region" aria-label={ru ? "История сделок" : "Trade history"}><table><thead><tr>
        <th>{t("journal.col.symbol")}</th><th>{ru ? "Закрыта · ET" : "Closed · ET"}</th><th>{t("journal.col.entry")}</th><th>{t("journal.col.exit")}</th><th>{t("journal.col.pnl")}</th><th>{t("journal.col.pct")}</th><th>{t("journal.col.strategy")}</th>
      </tr></thead><tbody>{data.items.map((trade, i) => {
        const pnl = numeric(trade.pnl);
        const date = trade.closed_at ? new Date(trade.closed_at) : null;
        return <tr key={trade.id ?? i}><td><div className={styles.identity}><span className={styles.symbolIcon}>{trade.symbol.slice(0,2)}</span><div><strong>{trade.symbol}</strong>{trade.name && <small>{trade.name}</small>}</div></div></td>
          <td>{date && !Number.isNaN(date.getTime()) ? date.toLocaleString(ru ? "ru-RU" : "en-US", {timeZone: "America/New_York", year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}) : "—"}</td>
          <td>{price(trade.entry)}</td><td>{price(trade.exit)}</td><td><span className={styles.pnl} data-tone={pnl === null || pnl === 0 ? "neutral" : pnl > 0 ? "positive" : "negative"}>{price(trade.pnl, true)}</span></td><td>{numeric(trade.pnl_pct) === null ? "—" : `${price(trade.pnl_pct, true)}%`}</td><td><span className={styles.strategy}>{trade.strategy_version || "—"}</span></td></tr>;
      })}</tbody></table></div>}
      {data && <footer className={styles.footer}><span>{loading || error ? "—" : `${data.total ? (data.page - 1) * data.page_size + 1 : 0}–${Math.min(data.page * data.page_size, data.total)}`} {ru ? "из" : "of"} {data.total}</span><TablePager pager={pager} /></footer>}
    </section>
    {!!r?.notes?.length && <details className={styles.notes}><summary>{t("journal.notes")}<span>{r.notes.length}</span></summary><ul>{r.notes.map((note, i) => <li key={i}>{note}</li>)}</ul></details>}
  </div>;
}
