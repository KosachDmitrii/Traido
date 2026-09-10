import { useEffect, useId, useState } from "react";
import { fetchOrbSymbol, type OrbSymbolView } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { Button, LoadingDots } from "@/ui";
import { orbReason, orbState, px } from "./orbLabels";
import styles from "@/pages/EvaluationPage.module.css";

export function OrbSymbolInspector() {
  const { desk } = useDesk();
  const { locale } = useI18n();
  const ru = locale === "ru";
  const listId = useId();
  const symbols = [...new Set([...(desk?.scanner.universe ?? []), ...Object.keys(desk?.orb?.plans ?? {})])].sort();
  const [input, setInput] = useState("");
  const [selected, setSelected] = useState("");
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<OrbSymbolView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!selected && symbols[0]) { setSelected(symbols[0]); setInput(symbols[0]); }
  }, [selected, symbols[0]]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setData(null); setLoading(true); setError(false);
    fetchOrbSymbol(selected, controller.signal).then(next => { if (!controller.signal.aborted) setData(next); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [selected, revision]);
  const time = (value?: string | null) => {
    const d = value ? new Date(value) : null;
    return d && !Number.isNaN(d.getTime()) ? d.toLocaleString(ru ? "ru-RU" : "en-US", {timeZone:"America/New_York", hour12:false}) + " ET" : "—";
  };
  const plan = data?.plan;
  const metrics = [
    ["Bid · USD", px(data?.quote?.bid)], ["Ask · USD", px(data?.quote?.ask)],
    [ru ? "Диапазон открытия" : "Opening range", plan ? `${px(plan.range_low)}–${px(plan.range_high)}` : "—"],
    [ru ? "Уровень пробоя" : "Breakout trigger", px(plan?.trigger)],
    [ru ? "Стоп" : "Stop", px(plan?.stop)], [ru ? "Максимальная цена входа" : "Maximum entry", px(plan?.max_entry)],
    [ru ? "Относительный объём" : "Relative volume", plan ? `${px(plan.relative_volume)}×` : "—"],
    ["ATR14", px(plan?.daily_atr)],
  ];
  const reasons = data?.rejections.length ? data.rejections : data?.state?.reasons ?? [];
  return <section className={styles.card}>
    <div className={styles.sectionHead}><h2>{ru ? "Просмотр акции" : "Symbol inspection"}</h2><span>{data?.session ?? "—"}</span></div>
    <form className={styles.symbolForm} onSubmit={e => { e.preventDefault(); const value=input.trim().toUpperCase(); if (/^[A-Z][A-Z0-9.\-]{0,15}$/.test(value)) {setSelected(value);setRevision(n=>n+1);} }}>
      <label htmlFor={`${listId}-input`}>{ru ? "Выберите или введите тикер" : "Choose or enter a symbol"}</label>
      <input id={`${listId}-input`} list={listId} value={input} onChange={e=>setInput(e.target.value.toUpperCase())} placeholder="AAPL" required pattern="[A-Za-z][A-Za-z0-9.\-]{0,15}" maxLength={16} autoComplete="off" />
      <datalist id={listId}>{symbols.map(symbol => <option key={symbol} value={symbol} />)}</datalist>
      <Button type="submit" variant="ghost" disabled={loading} aria-busy={loading}>{loading && <LoadingDots ariaLabel={ru ? "Загрузка" : "Loading"} />}{ru ? "Посмотреть / обновить" : "View / refresh"}</Button>
    </form>
    {loading ? <div className={styles.loading}><LoadingDots ariaLabel={ru ? "Загружаем данные акции" : "Loading symbol"} /><span>{ru ? "Загружаем данные акции…" : "Loading symbol…"}</span></div> : error ? <p className={styles.notice} role="alert">{ru ? "Не удалось получить данные. Попробуйте обновить." : "Could not load data. Please retry."}</p> : data ? <>
      <div className={styles.sectionHead}><h2>{data.symbol}</h2><span>{data.state ? orbState(data.state.state) : data.outranked ? (ru ? "Не вошла в топ-20" : "Outside top 20") : data.rejections.length ? (ru ? "Исключена из отбора" : "Excluded") : (ru ? "Решение ещё не получено" : "No decision reported")}</span></div>
      <div className={styles.symbolMetrics}>{metrics.map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
      <p className={styles.description}>Alpaca {data.quote?.feed?.toUpperCase() ?? "—"} · {ru ? "Котировка от" : "Quote timestamp"} {time(data.quote?.ts)}</p>
      {data.quote_error && <p className={styles.notice}>{orbReason(data.quote_error)}</p>}
      {data.outranked && <p className={styles.notice}>{ru ? "Условия ORB пройдены, но другие акции получили более высокий относительный объём." : "ORB conditions passed, but other symbols ranked higher by relative volume."}</p>}
      {reasons.map(reason=><p className={styles.notice} key={reason}>{orbReason(reason)}</p>)}
      {!plan && !reasons.length && !data.outranked && <p className={styles.notice}>{data.session_reason ? orbReason(data.session_reason) : (ru ? "Сохранённого плана или причины исключения для этого тикера нет." : "No saved plan or exclusion reason for this symbol.")}</p>}
      {plan && <p className={styles.description}>{ru ? "Вход до" : "Entry deadline"} {time(plan.entry_deadline)} · {ru ? "Выход до" : "Exit deadline"} {time(plan.exit_at)}</p>}
      <p className={styles.description}>{ru ? "Котировка — снимок на указанное время. Обновление цены не пересчитывает решение ORB. Состояние плана обновлено:" : "The quote is a snapshot at the displayed time. Refreshing it does not recalculate the ORB decision. Plan state observed:"} {time(data.state?.observed_at)}</p>
    </> : <p className={styles.description}>{ru ? "Введите тикер для просмотра." : "Enter a symbol to inspect."}</p>}
  </section>;
}
