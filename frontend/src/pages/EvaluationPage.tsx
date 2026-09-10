import { OrbSymbolInspector } from "@/components/desk/OrbSymbolInspector";
import { useEffect, useState } from "react";
import { BarChart3, RefreshCw, Target, TrendingUp } from "lucide-react";
import { fetchOrbEvaluation, type OrbEvaluation } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { Button, TablePager, useTablePager } from "@/ui";
import { orbReason, orbState, px } from "@/components/desk/orbLabels";
import styles from "./EvaluationPage.module.css";

export function EvaluationPage() {
  const { desk } = useDesk();
  const { locale } = useI18n();
  const ru = locale === "ru";
  const [result, setResult] = useState<OrbEvaluation | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError(false);
    fetchOrbEvaluation(abort.signal).then(data => { if (!abort.signal.aborted) setResult(data); })
      .catch(() => { if (!abort.signal.aborted) setError(true); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [revision, desk?.review?.trade_count]);
  const orb = desk?.orb;
  const plans = Object.values(orb?.plans ?? {});
  const pager = useTablePager(plans);
  const counts = orb?.counts;
  const stages = [
    [ru ? "Инструменты" : "Universe", counts?.universe],
    [ru ? "Допущены по типу" : "Eligible assets", counts?.eligible],
    [ru ? "Проверены диапазоны" : "Ranges evaluated", counts?.opening_evaluated],
    [ru ? "Прошли условия ORB" : "ORB qualified", counts?.qualified],
    [ru ? "Отобраны планы" : "Selected plans", counts?.selected],
  ];
  const shown = !error && !loading ? result : null;
  const stats = [
    { icon: BarChart3, label: ru ? "Закрытых сделок ORB" : "Closed ORB trades", value: shown?.trade_count ?? "—" },
    { icon: TrendingUp, label: ru ? "P&L журнала · USD" : "Journal P&L · USD", value: px(shown?.pnl) },
    { icon: Target, label: ru ? "Доля прибыльных" : "Win rate", value: shown?.win_rate == null ? "—" : `${(shown.win_rate * 100).toFixed(1)}%` },
    { icon: TrendingUp, label: ru ? "Средняя сделка · USD" : "Average trade · USD", value: px(shown?.expectancy) },
  ];
  return <div className={styles.page}>
    <header className={styles.heading}><div><span className={styles.eyebrow}>ORB · ALPACA PAPER</span><h1>{ru ? "Оценка стратегии" : "Strategy evaluation"}</h1><p>{ru ? "Отбор текущей сессии и фактические результаты ORB." : "Current session selection and actual ORB results."}</p></div><Button variant="ghost" disabled={loading} onClick={() => setRevision(n => n + 1)}><RefreshCw size={14} />{ru ? "Обновить результаты" : "Refresh results"}</Button></header>
    <OrbSymbolInspector />
    <section className={styles.card}>
      <div className={styles.sectionHead}><h2>{ru ? "Результаты Paper" : "Paper results"}</h2><span>{result?.strategy_version ?? "ORB"}</span></div>
      <p className={styles.description}>{ru ? "Только закрытые сделки текущей версии ORB из журнала. Старые стратегии и бэктесты исключены. Источники IEX/SIP в этой сводке не разделены." : "Closed journal trades for the current ORB version only. Legacy strategies and backtests are excluded. IEX/SIP results are not separated in this summary."}</p>
      {error && <p className={styles.notice} role="alert">{ru ? "Результаты не удалось загрузить. Нажмите «Обновить результаты»." : "Could not load results. Select Refresh results."}</p>}
      <div className={styles.stats} aria-busy={loading}>{stats.map(({icon: Icon, label, value}) => <div key={label}><span><Icon size={15} aria-hidden />{label}</span><strong>{value}</strong></div>)}</div>
      <div className={styles.outcomes}><span>{ru ? "Прибыль / убыток / без изменения" : "Wins / losses / breakeven"}: <b>{shown ? `${shown.wins} / ${shown.losses} / ${shown.breakeven}` : "—"}</b></span><span>Profit factor: <b>{px(shown?.profit_factor)}</b></span></div>
      <p className={styles.description}>{shown?.trade_count === 0 ? (ru ? "Закрытых сделок ORB пока нет — оценивать доходность ещё не по чему." : "No closed ORB trades yet; there are no outcomes to evaluate.") : (ru ? "Profit factor не рассчитывается без убыточных сделок. Результаты Paper сами по себе не подтверждают готовность к Live." : "Profit factor is undefined without losing trades. Paper outcomes alone do not establish live readiness.")}</p>
    </section>
    <section className={styles.card}><div className={styles.sectionHead}><h2>{ru ? "Отбор сессии" : "Session selection"}</h2><span>{orb?.session ?? "—"} · Alpaca {orb?.feed?.toUpperCase() ?? "—"}</span></div>
      {orb?.reason && <p className={styles.notice}>{orbReason(orb.reason)}</p>}
      <div className={styles.funnel}>{stages.map(([label,value]) => <div key={label}><span>{label}</span><strong>{value ?? "—"}</strong></div>)}</div>
      <details className={styles.details}><summary>{ru ? "Причины исключения" : "Exclusion reasons"}</summary>{Object.entries(orb?.rejection_counts ?? {}).length ? Object.entries(orb?.rejection_counts ?? {}).map(([reason,count]) => <div className={styles.reason} key={reason}><span>{orbReason(reason)}</span><b>{count}</b></div>) : <p className={styles.description}>{ru ? "Причины исключения пока не получены." : "No exclusion counts have been reported yet."}</p>}</details>
    </section>
    <section className={styles.card}><div className={styles.sectionHead}><h2>{ru ? "Планы и состояния" : "Plans & states"}</h2><span>{plans.length}</span></div>
      {!plans.length ? <p className={styles.description}>{ru ? "Планы появятся после отбора. Отсутствие планов не означает убыточную стратегию." : "Plans appear after selection. No plans does not imply a losing strategy."}</p> : <div className={styles.scroll}><table><thead><tr>{(ru ? ["Тикер","Пробой","Стоп","Макс. вход","Отн. объём","Состояние"] : ["Symbol","Trigger","Stop","Max entry","Rel. volume","State"]).map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{pager.slice.map(plan => <tr key={plan.symbol}><td><strong>{plan.symbol}</strong></td><td>{px(plan.trigger)}</td><td>{px(plan.stop)}</td><td>{px(plan.max_entry)}</td><td>{px(plan.relative_volume)}×</td><td><span className={styles.status}>{orbState(orb?.states?.[plan.symbol]?.state)}</span></td></tr>)}</tbody></table></div>}
      <TablePager pager={pager} />
    </section>
    <div className={styles.note}><strong>{ru ? "Исторический тест ORB ещё не реализован" : "Historical ORB backtest is not implemented"}</strong><p>{ru ? "Здесь нет результатов старого trader_desk, F3 или сравнения со SPY. Для корректного бэктеста ORB нужны внутридневные данные диапазона открытия и моделирование исполнения; дневной тест прежней стратегии их не заменяет." : "Legacy trader_desk, F3 and SPY comparisons have been removed from this page. A valid ORB backtest requires intraday opening-range data and execution modelling; the old daily-bar strategy test cannot substitute for it."}</p></div>
  </div>;
}
