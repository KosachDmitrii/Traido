import type { DeskResponse } from "@/lib/api";
import { orbReason } from "./orbLabels";
export function SessionDecisionStrip({ desk }: { desk: DeskResponse | null }) {
  if (!desk?.light_available) return null;
  const states = Object.values(desk.orb?.states ?? {});
  const observation = desk.orb?.observation;
  return <section className="session-strip" aria-label="Состояние ORB">
    <span className="session-strip__item">Предложения купить <b>{desk.buy_opportunities?.length ?? 0}</b></span>
    <span className="session-strip__item">Планы ORB <b>{Object.keys(desk.orb?.plans ?? {}).length}</b></span>
    <span className="session-strip__item">Ждут пробоя <b>{states.filter(s=>s.state === "WAIT" && s.reasons?.includes("ORB_RETEST_WAIT_BREAKOUT")).length}</b></span>
    <span className="session-strip__item">Проверка данных заблокирована <b>{states.filter(s=>s.state === "DATA_BLOCKED").length}</b></span>
    {desk.orb?.observation && <span className="session-strip__item">Ожидают актуализации <b>{desk.orb.observation.pending}</b></span>}
    <span className="session-strip__item">Открытые позиции <b>{desk.positions?.length ?? 0}</b></span>
    {observation && <details className="session-strip__note" style={{flexBasis:"100%"}}>
      <summary>Проверены за последние 5 минут: {observation.checked_recently} из {observation.total} · причины блокировки данных</summary>
      <p>Это время проверки плана. Возраст котировки и полнота свечей проверяются отдельно перед покупкой.</p>
      {Object.entries(observation.blocked_reasons).sort((a,b)=>b[1]-a[1]).map(([reason,n])=><p key={reason}>{orbReason(reason)}: <b>{n}</b></p>)}
      {!Object.keys(observation.blocked_reasons).length && <p>Блокировок данных нет.</p>}
    </details>}
  </section>;
}
