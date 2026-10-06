import { useEffect, useState } from "react";
import { fetchForwardMonitoring, type ForwardMonitoring as Report } from "@/lib/api";
import { useI18n } from "@/i18n/I18nProvider";
import { TablePager, useTablePager } from "@/ui";
import { orbReason, px } from "./orbLabels";
import styles from "@/pages/EvaluationPage.module.css";

export function ForwardMonitoring() {
  const { locale } = useI18n();
  const ru = locale === "ru";
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active: AbortController | null = null;
    let disposed = false;
    const load = () => {
      active?.abort();
      const abort = new AbortController();
      active = abort;
      fetchForwardMonitoring(abort.signal).then(data => {
        if (!disposed && !abort.signal.aborted) { setReport(data); setError(false); }
      }).catch(() => { if (!disposed && !abort.signal.aborted) setError(true); });
    };
    load();
    const timer = window.setInterval(load, 60_000);
    return () => { disposed = true; window.clearInterval(timer); active?.abort(); };
  }, []);
  const ready = report?.available && !report.stale && report.observer.running && !report.observer.last_error && !error;
  const verdict = (status?: string) => !ready ? (ru ? "Нет свежего отчёта" : "No fresh report") : status === "passed" ? (ru ? "Критерии выполнены" : "Criteria met") : status === "failed" ? (ru ? "Есть нарушения" : "Issues detected") : (ru ? "Недостаточно данных" : "Insufficient evidence");
  const pager = useTablePager([...(report?.daily ?? [])].reverse());
  const funnel = [...(report?.funnel ?? [])].sort((a, b) => b.count - a.count).slice(0, 20);
  return <section className={styles.card}>
    <div className={styles.sectionHead}><h2>{ru ? "Постоянный контроль · 30 сессий" : "Continuous monitoring · 30 sessions"}</h2><span>{ready ? (ru ? "Наблюдение работает" : "Observer running") : (ru ? "Ожидаем свежие данные" : "Awaiting fresh evidence")}</span></div>
    <p className={styles.description}>{ru ? "Наблюдения сохраняются каждую минуту, отчёт обновляется каждые 5 минут, даже если эта страница закрыта. Проверки анализируют работу приложения и не управляют сделками." : "Observations are saved every minute and the report updates every 5 minutes, even with this page closed. Checks analyse the application without controlling trades."}</p>
    {error && <p role="alert" className={styles.notice}>{ru ? "Не удалось обновить отчёт. Повторная попытка выполняется автоматически." : "Report update failed. Retrying automatically."}</p>}
    <div className={`${styles.stats} ${styles.monitoringStats}`}>
      <div><span>{ru ? "Техническая исправность" : "Technical integrity"}</span><strong>{verdict(report?.technical_status)}</strong></div>
      <div><span>{ru ? "Соблюдение бюджета" : "Budget compliance"}</span><strong>{verdict(report?.budget_status)}</strong></div>
      <div><span>{ru ? "Результативность стратегии" : "Strategy performance"}</span><strong>{verdict(report?.strategy_status)}</strong></div>
    </div>
    <p>{ru ? "Полностью наблюдаемых сессий" : "Fully observed sessions"}: {ready ? report.complete_sessions : "—"} / 30 · {ru ? "Открытых позиций" : "Open positions"}: {ready ? report.open_positions?.length : "—"}</p>
    <p className={styles.description}>{ru ? "Изменение торговых параметров начинает новый период наблюдения. 30 сессий и 100 закрытых сделок на версию — контрольные точки, а не доказательство будущей прибыли." : "Changed trading parameters start a new observation cohort. 30 sessions and 100 closed trades per version are checkpoints, not proof of future profit."}</p>
    {ready && <>
      <p>{ru ? "Капитал при наблюдении" : "Observed account equity"}: ${px(report.account?.equity)} · {ru ? "Изменение капитала за день" : "Daily equity change"}: ${px(report.account?.day_pnl)} · {ru ? "Наблюдаемая просадка счёта" : "Sampled account drawdown"}: {report.sampled_account_drawdown_pct == null ? "—" : `${report.sampled_account_drawdown_pct.toFixed(2)}%`}</p>
      <p>{ru ? "Проверки заявок по сохранённым лимитам" : "Entry checks against saved limits"}: {ru ? "прошли" : "passed"} {report.budget_checks?.passed ?? 0} · {ru ? "нарушения" : "failed"} {report.budget_checks?.failed ?? 0} · {ru ? "нет доказательств" : "missing evidence"} {report.budget_checks?.insufficient_data ?? 0}</p>
      {report.strategies?.map(s => <p key={s.strategy_version}>{s.strategy_version}: {s.closed_trades} / 100 {ru ? "закрытых сделок в текущем периоде" : "closed trades in current cohort"} · {ru ? "P&L до расходов" : "P&L before costs"}: ${px(s.gross_closed_pnl)}</p>)}
    </>}
    <p className={styles.notice}>{ru ? "Чистая доходность пока не подтверждена: не записаны все расходы, исторические оценки открытых позиций и движения средств. Проверка общего резерва для одновременных заявок и сценариев малого счёта тоже ещё не завершена. Положительный P&L закрытых сделок не означает прибыль всего счёта." : "Net performance is not established: complete costs, historical open-position valuations and cash flows are missing. Aggregate pending reserves and small-account scenarios are not yet proven. Positive closed-trade P&L does not imply an account profit."}</p>
    {ready && <>
      <details className={styles.details}><summary>{ru ? "Ежедневный отчёт" : "Daily report"}</summary><p className={styles.description}>{ru ? "История журнала включает все версии Paper. Результаты текущего периода выше рассчитываются отдельно. Прочерк означает, что нет сделок в журнале и достаточных наблюдений." : "Journal history includes all Paper strategy versions. Current cohort results above are separate. A dash means there are no journal trades and insufficient observations."}</p><div className={styles.scroll}><table><thead><tr>{(ru ? ["Сессия", "Наблюдение", "Техника", "Предложения", "Заявки входа", "Закрытые сделки", "Непроверенные", "P&L до расходов"] : ["Session", "Coverage", "Technical", "Proposals", "Entry intents", "Closed trades", "Unverified", "P&L before costs"]).map(s => <th key={s}>{s}</th>)}</tr></thead><tbody>{pager.slice.map(d => <tr key={d.session}><td>{d.session}</td><td>{d.complete ? (ru ? "Полное" : "Complete") : (ru ? "Есть пропуски" : "Incomplete")}</td><td>{verdict(d.technical_status)}</td><td>{d.proposals}</td><td>{d.entry_intents}</td><td>{d.closed_trades}</td><td>{d.unverified_trades}</td><td>${px(d.gross_closed_pnl)}</td></tr>)}</tbody></table></div><TablePager pager={pager} /></details>
      <details className={styles.details}><summary>{ru ? "Этапы и причины решений" : "Decision stages and reasons"}</summary><p className={styles.description}>{ru ? "Счётчики включают повторные проверки, это не число уникальных предложений. Показаны 20 наиболее частых причин за период." : "Counts include repeated evaluations, not unique proposals. Top 20 reasons for the period."}</p>{funnel.length ? funnel.map(f => <div className={styles.reason} key={`${f.stage}:${f.outcome}:${f.reason}`}><span>{f.stage} · {orbReason(f.reason)}</span><b>{f.count}</b></div>) : <p>{ru ? "Сохранённых решений пока нет." : "No saved decisions yet."}</p>}</details>
    </>}
    {report?.generated_at && <p className={styles.description}>{ru ? "Отчёт обновлён" : "Report updated"}: {new Date(report.generated_at).toLocaleString(locale)}</p>}
  </section>;
}
