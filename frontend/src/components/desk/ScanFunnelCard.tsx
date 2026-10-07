import { useDesk } from "@/context/DeskContext";
import { orbReason } from "./orbLabels";

export function ScanFunnelCard() {
  const { desk } = useDesk();
  const orb = desk?.orb;
  const c = orb?.counts ?? {};
  const states = Object.values(orb?.states ?? {});
  const rows: [string, number | undefined][] = [
    ["Вселенная", c.universe], ["Допущены по типу инструмента", c.eligible],
    ["Недостаточно дневной истории", c.history_missing], ["Не прошли объём или ATR", c.base_rejected],
    ["Проверены диапазоны открытия", c.opening_evaluated], ["Прошли условия ORB", c.qualified],
    ["Все планы, прошедшие отбор", c.selected],
    ["Ждут пробоя", states.filter(s=>s.state === "WAIT" && s.reasons?.includes("ORB_RETEST_WAIT_BREAKOUT")).length],
    ["Ждут возврата", states.filter(s=>s.state === "WAIT" && s.reasons?.includes("ORB_RETEST_WAIT_RETURN")).length],
    ["Ждут подтверждения", states.filter(s=>s.state === "WAIT" && s.reasons?.includes("ORB_RETEST_WAIT_CONFIRMATION")).length],
    ["Проверка данных заблокирована", states.filter(s=>s.state === "DATA_BLOCKED").length],
    ["Вход заблокирован", states.filter(s=>s.state === "BLOCKED").length],
    ["Предложения для подтверждения", desk?.buy_opportunities?.length ?? 0],
  ];
  return <section className="panel" style={{padding:24}}>
    <h2>Отбор ORB · {orb?.session ?? "текущая сессия"}</h2>
    <p>Alpaca {(orb?.feed ?? "sip").toUpperCase()} · открытие и новые пятиминутные диапазоны · история 14 сессий · только покупки</p>
    {orb?.intraday_discovery?.evaluated_at && <p role="status">Внутридневной отбор: {orb.intraday_discovery.status === "ready" ? "работает" : "данные недоступны"} · проверено {orb.intraday_discovery.checked ?? "—"} бумаг · добавлено {orb.intraday_discovery.added?.length ?? 0} · {new Date(orb.intraday_discovery.evaluated_at).toLocaleTimeString("ru-RU", {hour12:false})}</p>}
    {orb?.intraday_discovery?.reason && <p role="status">{orbReason(orb.intraday_discovery.reason)}</p>}
    {orb?.reason && <p role="status">{orbReason(orb.reason)}</p>}
    {orb?.observation && <p role="status">Проверены за последние 5 минут: {orb.observation.checked_recently} из {orb.observation.total}. Ожидают актуализации: {orb.observation.pending}. Проверка плана не означает, что все его рыночные данные свежие.</p>}
    {!!Object.keys(orb?.observation?.blocked_reasons ?? {}).length && <details open><summary>Почему проверка данных заблокирована</summary>{Object.entries(orb?.observation?.blocked_reasons ?? {}).sort((a,b)=>b[1]-a[1]).map(([reason,n])=><p key={reason}>{orbReason(reason)}: <b>{n}</b></p>)}</details>}
    <table style={{width:"100%"}}><tbody>{rows.map(([label,value])=><tr key={label}><td style={{padding:"8px 0"}}>{label}</td><td style={{textAlign:"right"}}>{value ?? "—"}</td></tr>)}</tbody></table>
    <details><summary>Условия отбора</summary><p>Открытие выше $5; средний дневной объём SIP ≥ 1 млн акций; ATR14 &gt; $0.50; объём первых пяти минут ≥ среднего объёма этого окна за 14 сессий; растущая первая свеча. Наблюдаем все прошедшие отбор планы. Каждые пять минут дополнительно проверяем ещё не отобранные ликвидные бумаги: растущую закрытую свечу и её объём относительно того же времени за 14 прошлых сессий. Новый диапазон требует нового пробоя, возврата и подтверждения; сам отбор не разрешает покупку. Внутридневная стратегия экспериментальная, только Paper. Для ORB «Возврат» последовательно ждём пробой, возврат и подтверждение по закрытым пятиминутным свечам. Перед покупкой повторно проверяем цену и условия входа. Действующий режим выхода указан в карточке сделки.</p></details>
    <details><summary>Причины исключения</summary>{Object.entries(orb?.rejection_counts ?? {}).map(([reason,n])=><p key={reason}>{orbReason(reason)}: <b>{n}</b></p>)}</details>
  </section>;
}
