import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { RefreshCw, ShieldCheck, Target, Clock3 } from "lucide-react";
import { fetchStrategyPassport, type StrategyPassport } from "@/lib/api";
import { useI18n } from "@/i18n/I18nProvider";
import { Button, LoadingDots } from "@/ui";
import { px } from "@/components/desk/orbLabels";
import styles from "./StrategiesPage.module.css";

export function StrategiesPage() {
  const { locale } = useI18n();
  const ru = locale === "ru";
  const [data, setData] = useState<StrategyPassport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(false);
    fetchStrategyPassport(controller.signal).then(next => { if (!controller.signal.aborted) setData(next); })
      .catch(() => { if (!controller.signal.aborted) setError(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  const p = data?.parameters ?? {};
  const v = (key: string) => p[key] == null ? "—" : String(p[key]);
  const rules = [
    [ru ? "Диапазон открытия" : "Opening range", `${v("opening_minutes")} ${ru ? "минут после открытия" : "minutes after open"}`],
    [ru ? "История для сравнения" : "Lookback", `${v("lookback_sessions")} ${ru ? "сессий" : "sessions"}`],
    [ru ? "Цена открытия" : "Opening price", `> $${v("min_price")}`],
    [ru ? "Дневной ATR" : "Daily ATR", `> $${v("min_daily_atr")}`],
    [data?.feed === "iex" ? (ru ? "Средний дневной оборот IEX" : "Average daily IEX turnover") : (ru ? "Средний дневной объём SIP" : "Average daily SIP volume"), data?.feed === "iex" ? `≥ $${v("iex_min_avg_dollar_volume")}` : `≥ ${v("sip_min_daily_volume")} ${ru ? "акций" : "shares"}`],
    [ru ? "Относительный объём окна открытия" : "Opening relative volume", `≥ ${v("min_relative_volume")}×`],
    [ru ? "Направление первой свечи" : "First candle direction", ru ? "Растущая: закрытие выше открытия" : "Bullish: close above open"],
    [ru ? "Отбор по относительному объёму" : "Relative volume selection", ru ? "Все прошедшие отбор" : "All qualifying plans"],
  ];
  return <div className={styles.page}>
    <header className={styles.heading}><div><span className={styles.eyebrow}>ORB</span><h1>{ru ? "Паспорт стратегии" : "Strategy passport"}</h1><p>{ru ? "Действующие правила и состояние проверки стратегии." : "Active rules and strategy verification status."}</p></div><Button variant="ghost" loading={loading} onClick={()=>setRevision(n=>n+1)}>{!loading && <RefreshCw size={14} />}{ru ? "Обновить" : "Refresh"}</Button></header>
    {loading ? <div className={styles.message}><LoadingDots ariaLabel={ru ? "Загрузка паспорта" : "Loading passport"} /></div> : error || !data ? <p className={styles.message} role="alert">{ru ? "Не удалось загрузить паспорт. Повторите обновление." : "Could not load the passport. Retry refresh."}</p> : <>
      <div className={styles.summary}>{[
        [ru ? "Версия" : "Version", data.version],
        [ru ? "Котировки" : "Market data", `Alpaca ${data.feed.toUpperCase()}`],
        [ru ? "Среда брокера" : "Broker environment", data.broker_env],
        [ru ? "Режим решений" : "Decision mode", data.trading_mode],
      ].map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
      <section className={styles.card}><h2><Target size={18} />{ru ? "Условия отбора" : "Selection rules"}</h2><p>{ru ? "Только покупки. Значения загружены из параметров действующей ORB." : "Long only. Values come from the active ORB parameters."}</p><dl className={styles.rules}>{rules.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></section>
      <div className={styles.columns}>
        <section className={styles.card}><h2><Target size={18} />{ru ? "Вход и риск" : "Entry & risk"}</h2><ul>
          <li>{ru ? "Триггер: максимум диапазона открытия + $0.01." : "Trigger: opening-range high + $0.01."}</li>
          <li>{ru ? "Стоп ниже триггера на" : "Stop below trigger by"} {v("stop_atr_fraction")} ATR.</li>
          <li>{ru ? "Допуск выше цены входа" : "Maximum premium above trigger"}: {v("max_entry_drift_r")}R — {ru ? "R здесь — расстояние от цены входа до стопа. Экспериментальный допуск для Paper." : "R is the distance from the trigger to the stop. Experimental Paper allowance."}</li>
          <li>{ru ? "Количество акций определяется проверкой риска счёта; параметры плана фиксируются после отбора." : "Account risk checks determine quantity; plan parameters are fixed after selection."}</li>
          <li>{ru ? "Перед отправкой проверяются котировка, цена, доступность входа и риск. Наличие плана ещё не разрешает покупку." : "Quote, price, entry availability and risk are checked before submission. A plan alone does not authorize a purchase."}</li>
        </ul></section>
        <section className={styles.card}><h2><Clock3 size={18} />{ru ? "Выход и время" : "Exit & timing"}</h2><ul>
          <li>{ru ? "Вход после завершения диапазона открытия." : "Entry after the opening range is complete."}</li>
          <li>{ru ? "Плановый выход за" : "Scheduled exit"} {v("exit_buffer_seconds")} {ru ? "секунд до закрытия сессии." : "seconds before session close."}</li>
          <li>{ru ? "Новые входы прекращаются за" : "New entries end"} {v("entry_cutoff_minutes_before_exit")} {ru ? "минут до планового выхода." : "minutes before scheduled exit."}</li>
          <li>{ru ? "Выход по стопу или времени. Фиксированной цели прибыли нет. Время учитывает сокращённые сессии." : "Exit by stop or time. No fixed profit target. Timing accounts for shortened sessions."}</li>
        </ul></section>
      </div>
      <section className={styles.card}><h2><ShieldCheck size={18} />{ru ? "Состояние проверок" : "Verification status"}</h2><dl className={styles.rules}>
        <div><dt>{ru ? "Правила ORB в приложении" : "ORB rules in the application"}</dt><dd>{ru ? "Реализованы" : "Implemented"}</dd></div>
        <div><dt>{ru ? "Закрытые сделки этой версии" : "Closed trades for this version"}</dt><dd>{data.paper.trade_count}</dd></div>
        <div><dt>{ru ? "Исторический тест ORB" : "Historical ORB backtest"}</dt><dd>{ru ? "Не реализован" : "Not implemented"}</dd></div>
        <div><dt>{ru ? "Проверка вне выборки / walk-forward" : "Out-of-sample / walk-forward"}</dt><dd>{ru ? "Для ORB не проведена" : "Not performed for ORB"}</dd></div>
        <div><dt>{ru ? "Готовность к Live" : "Live readiness"}</dt><dd>{ru ? "Не подтверждена" : "Not certified"}</dd></div>
        <div><dt>allow_live_trading</dt><dd>{String(data.allow_live_trading)}</dd></div>
      </dl><p>{ru ? "Реализация правил и наличие Paper-сделок не являются доказательством прибыльности." : "Implemented rules and Paper trades do not establish profitability."}</p></section>
      <section className={styles.card}><div className={styles.sectionHead}><h2>{ru ? "Результаты Paper" : "Paper results"}</h2><Link to="/evaluation">{ru ? "Открыть оценку →" : "View evaluation →"}</Link></div><div className={styles.summary}>
        <div><span>{ru ? "Сделки" : "Trades"}</span><strong>{data.paper.trade_count}</strong></div>
        <div><span>P&L · USD</span><strong>{px(data.paper.pnl)}</strong></div>
        <div><span>{ru ? "Прибыльных" : "Win rate"}</span><strong>{data.paper.win_rate == null ? "—" : `${(data.paper.win_rate*100).toFixed(1)}%`}</strong></div>
        <div><span>{ru ? "Средняя сделка · USD" : "Average trade · USD"}</span><strong>{px(data.paper.expectancy)}</strong></div>
      </div><p>{ru ? "Только текущая версия ORB; старые стратегии и бэктесты исключены. Результаты IEX/SIP здесь объединены." : "Current ORB version only; legacy strategies and backtests excluded. IEX/SIP results are combined here."}</p></section>
    </>}
  </div>;
}
