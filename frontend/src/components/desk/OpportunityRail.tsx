import { useState } from "react";
import type { DeskResponse, BuyOpportunity } from "@/lib/api";
import { decideBuy } from "@/lib/api";
import { humanizeError, type FlashMessage } from "@/lib/messages";
import type { FlashSlot } from "@/lib/toasts";
import { Button } from "@/ui";
import { orbReason, orbState, px, etTime } from "./orbLabels";

type Props = { desk: DeskResponse | null; onFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  onRefresh: () => Promise<void> };

export function OpportunityRail({ desk, onFlash, onRefresh }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const plans = Object.values(desk?.orb?.plans ?? {});
  const buys = desk?.buy_opportunities ?? [];
  async function decide(opp: BuyOpportunity, decision: "approve" | "skip", qty: number) {
    setBusy(opp.id);
    try {
      const result = await decideBuy(opp.id, decision, qty, { expectedDecisionVersion: opp.decision_version ?? 0 });
      onFlash({ kind: "ok", title: decision === "skip" ? "Предложение пропущено" : "Ответ на запрос покупки",
        detail: decision === "skip" ? "Повторного входа по этому плану сегодня не будет." : `Статус: ${result.status ?? "получен"}. Исполнение проверяется у брокера.` });
    } catch (error) {
      onFlash({ kind: "error", title: "Запрос не выполнен", detail: humanizeError(error instanceof Error ? error.message : String(error)).detail });
    } finally { setBusy(null); await onRefresh(); }
  }
  return <section className="opportunity-rail">
    <h2>Планы ORB</h2>
    <p>Пробой максимума 09:30–09:35 ET. Покупку подтверждаете вы. Выход — по стопу или до закрытия сессии.</p>
    {plans.length > 0 && desk?.orb?.reason && <p role="alert">{orbReason(desk.orb.reason)}</p>}
    {!plans.length && <p>{orbReason(desk?.orb?.reason ?? (desk?.orb?.status === "ready" ? "ORB_NO_CANDIDATES" : "ORB_LOADING"))}</p>}
    {plans.map(plan => {
      const state = desk?.orb?.states?.[plan.symbol];
      const opp = buys.find(o => o.candidate.symbol === plan.symbol && o.candidate.strategy_version === "orb@1.1.0");
      const maxQty = Math.max(0, Math.floor(Number(opp?.proposed_qty ?? opp?.risk?.sized_qty ?? 0)));
      const qty = Math.min(maxQty, quantities[plan.symbol] ?? maxQty);
      const buyable = !!opp && opp.viability?.buyable === true && desk?.session?.entries_allowed !== false && maxQty > 0;
      const reasons = opp && !opp.viability?.buyable ? opp.viability?.reasons ?? ["ORB_QUOTE_STALE"] : state?.reasons ?? ["ORB_WAITING_BREAKOUT"];
      const ask = opp?.viability?.measured?.ask ?? state?.ask;
      return <article key={plan.symbol} className="opp-card" style={{ border: "1px solid #dce1e8", borderRadius: 16, padding: 20, marginTop: 16 }}>
        <div style={{display:"flex",justifyContent:"space-between"}}><h3>{plan.symbol}</h3><strong>{buyable ? "Можно подтвердить" : orbState(state?.state)}</strong></div>
        <dl style={{display:"grid",gridTemplateColumns:"repeat(2,minmax(0,1fr))",gap:16}}>
          <div><dt>Сейчас · ask</dt><dd>{px(ask)}</dd></div><div><dt>Пробой</dt><dd>{px(plan.trigger)}</dd></div>
          <div><dt>Стоп</dt><dd>{px(plan.stop)}</dd></div><div><dt>Максимум покупки</dt><dd>{px(plan.max_entry)}</dd></div>
          <div><dt>Относительный объём</dt><dd>{Number(plan.relative_volume).toFixed(2)}×</dd></div><div><dt>Выход до</dt><dd>{etTime(plan.exit_at)} ET</dd></div>
        </dl>
        <p>Диапазон открытия: {px(plan.range_low)}–{px(plan.range_high)}. Вход до {etTime(plan.entry_deadline)} ET.</p>
        <p>{reasons.map(orbReason).join(" · ")}</p>
        {opp && <>
          <label>Кол-во <input aria-label={`Количество ${plan.symbol}`} type="number" min={1} max={maxQty} value={qty}
            onChange={e=>setQuantities(q=>({...q,[plan.symbol]:Math.max(1,Math.min(maxQty,Math.floor(Number(e.target.value)||1)))}))} /></label>
          <p>Риск при максимальной цене входа до стопа: ${((Number(plan.max_entry)-Number(plan.stop))*qty).toFixed(2)} плюс комиссии и проскальзывание. Стоп не гарантирует эту цену.</p>
          <Button disabled={!buyable || busy !== null || qty < 1} onClick={()=>void decide(opp,"approve",qty)}>Купить</Button>{" "}
          <Button disabled={busy !== null} onClick={()=>void decide(opp,"skip",qty)}>Пропустить</Button>
        </>}
      </article>;
    })}
  </section>;
}
