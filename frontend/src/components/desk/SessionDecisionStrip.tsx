import type { DeskResponse } from "@/lib/api";
export function SessionDecisionStrip({ desk }: { desk: DeskResponse | null }) {
  if (!desk) return null;
  const states = Object.values(desk.orb?.states ?? {});
  return <section className="session-strip" aria-label="Состояние ORB">
    <span className="session-strip__item">Предложения купить <b>{desk.buy_opportunities?.length ?? 0}</b></span>
    <span className="session-strip__item">Планы ORB <b>{Object.keys(desk.orb?.plans ?? {}).length}</b></span>
    <span className="session-strip__item">Ждут пробоя <b>{states.filter(s=>s.state === "WAIT" && s.reasons?.includes("ORB_RETEST_WAIT_BREAKOUT")).length}</b></span>
    <span className="session-strip__item">Нет данных <b>{states.filter(s=>s.state === "DATA_BLOCKED").length}</b></span>
    <span className="session-strip__item">Открытые позиции <b>{desk.positions?.length ?? 0}</b></span>
  </section>;
}
