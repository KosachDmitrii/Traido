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
    ["Ждут пробоя", states.filter(s=>s.state === "WAIT").length],
    ["Вход заблокирован", states.filter(s=>s.state === "BLOCKED" || s.state === "DATA_BLOCKED").length],
    ["Предложения для подтверждения", desk?.buy_opportunities?.length ?? 0],
  ];
  return <section className="panel" style={{padding:24}}>
    <h2>Отбор ORB · {orb?.session ?? "текущая сессия"}</h2>
    <p>Alpaca {(orb?.feed ?? "iex").toUpperCase()} · первые 5 минут · история 14 сессий · только покупки</p>
    {orb?.reason && <p role="status">{orbReason(orb.reason)}</p>}
    <table style={{width:"100%"}}><tbody>{rows.map(([label,value])=><tr key={label}><td style={{padding:"8px 0"}}>{label}</td><td style={{textAlign:"right"}}>{value ?? "—"}</td></tr>)}</tbody></table>
    <details><summary>Условия отбора</summary><p>Открытие выше $5; {orb?.feed === "sip" ? "средний дневной объём ≥ 1 млн акций" : "средний дневной оборот IEX ≥ $20 млн"}; ATR14 &gt; $0.50; объём первых пяти минут ≥ среднего объёма этого окна за 14 сессий; растущая первая свеча. Из прошедших выбираются 20 с наибольшим относительным объёмом. Вход — пробой максимума, стоп на 0.10 ATR ниже триггера, доплата к триггеру не более 0.25 планового риска на акцию.</p></details>
    <details><summary>Причины исключения</summary>{Object.entries(orb?.rejection_counts ?? {}).map(([reason,n])=><p key={reason}>{orbReason(reason)}: <b>{n}</b></p>)}</details>
  </section>;
}
