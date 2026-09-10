import { ArrowUpRight, Clock3, Layers, Radar, ShieldCheck } from "lucide-react";
import styles from "./OpportunityRail.module.css";
import { useState } from "react";
import type { DeskResponse, BuyOpportunity } from "@/lib/api";
import { decideBuy } from "@/lib/api";
import { humanizeError, type FlashMessage } from "@/lib/messages";
import type { FlashSlot } from "@/lib/toasts";
import { Button } from "@/ui";
import { orbReason, orbState, px, etTime } from "./orbLabels";

type Props = { desk: DeskResponse | null; onFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  onRefresh: () => Promise<void>; layout?: "rail" | "page" };

export function OpportunityRail({ desk, onFlash, onRefresh, layout = "rail" }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const plans = Object.values(desk?.orb?.plans ?? {});
  const buys = desk?.buy_opportunities ?? [];
  async function decide(opp: BuyOpportunity, decision: "approve" | "skip", qty: number) {
    setBusy(`${opp.id}:${decision}`);
    try {
      const result = await decideBuy(opp.id, decision, qty, { expectedDecisionVersion: opp.decision_version ?? 0 });
      onFlash({ kind: "ok", title: decision === "skip" ? "Предложение пропущено" : "Ответ на запрос покупки",
        detail: decision === "skip" ? "Повторного входа по этому плану сегодня не будет." : `Статус: ${result.status ?? "получен"}. Исполнение проверяется у брокера.` });
    } catch (error) {
      onFlash({ kind: "error", title: "Запрос не выполнен", detail: humanizeError(error instanceof Error ? error.message : String(error)).detail });
    } finally { setBusy(null); await onRefresh(); }
  }
  return <section className={layout === "page" ? styles.page : styles.rail}>
    {layout === "page" ? <>
      <header className={styles.heading}><div><span className={styles.eyebrow}>ORB · ALPACA PAPER</span><h1>Торговые возможности</h1><p>От ожидания пробоя до подтверждения покупки — каждый план перед глазами.</p></div><span className={styles.mode}><ShieldCheck size={15} />Ручное подтверждение</span></header>
      <div className={styles.summary}>
        <div><Layers size={18} /><span>Планы сессии</span><strong>{desk ? plans.length : "—"}</strong></div>
        <div><Clock3 size={18} /><span>Ждут цены входа</span><strong>{desk?.orb ? Object.values(desk.orb.states ?? {}).filter(s => s.state === "WAIT").length : "—"}</strong></div>
        <div><ArrowUpRight size={18} /><span>Предложения покупки</span><strong>{desk ? buys.filter(o => o.candidate.strategy_version === "orb@1.1.0").length : "—"}</strong></div>
      </div>
      <div className={styles.sectionHead}><h2>Планы ORB</h2><span>Диапазон открытия · 09:30–09:35 ET</span></div>
    </> : <>

    <header className={styles.railHead}><span className={styles.eyebrow}>ALPACA PAPER</span><h2>Планы ORB</h2><p>Диапазон открытия · 09:30–09:35 ET</p></header>
    </>}
    {plans.length > 0 && desk?.orb?.reason && <p className={styles.notice} role="alert">{orbReason(desk.orb.reason)}</p>}
    {!plans.length && <div className={styles.empty} role="status"><span className={styles.emptyIcon}><Radar size={28} /></span><h3>{!desk ? "Получаем торговые планы" : "Пока нет планов для входа"}</h3><p>{orbReason(desk?.orb?.reason ?? (desk?.orb?.status === "ready" ? "ORB_NO_CANDIDATES" : "ORB_LOADING"))}</p><span className={styles.emptyNote}>Планы появятся автоматически после отбора инструментов.</span></div>}
    <div className={styles.grid}>
    {plans.map(plan => {
      const state = desk?.orb?.states?.[plan.symbol];
      const opp = buys.find(o => o.candidate.symbol === plan.symbol && o.candidate.strategy_version === "orb@1.1.0");
      const maxQty = Math.max(0, Math.floor(Number(opp?.proposed_qty ?? opp?.risk?.sized_qty ?? 0)));
      const qty = Math.min(maxQty, quantities[plan.symbol] ?? maxQty);
      const buyable = !!opp && opp.viability?.buyable === true && desk?.session?.entries_allowed !== false && maxQty > 0;
      const reasons = opp && !opp.viability?.buyable ? opp.viability?.reasons ?? ["ORB_QUOTE_STALE"] : state?.reasons ?? ["ORB_WAITING_BREAKOUT"];
      const ask = opp?.viability?.measured?.ask ?? state?.ask;
      return <article key={plan.symbol} className={`opp-card ${styles.card}`} data-buyable={buyable}>
        <div className={styles.cardHead}><div className={styles.identity}><div><h3>{plan.symbol}</h3><span className={styles.strategy}>ORB · Покупка</span></div></div><strong className={styles.status}>{buyable ? "Можно подтвердить" : orbState(state?.state)}</strong></div>
        {(plan.name || opp?.candidate.name) && <p className={styles.companyName}>{plan.name || opp?.candidate.name}</p>}
        <dl className={styles.prices}>
          <div><dt>Цена сейчас</dt><dd>{px(ask)}</dd></div>
          <div><dt>Цена входа</dt><dd>{px(plan.trigger)}</dd></div>
          <div><dt>Защитный стоп</dt><dd>{px(plan.stop)}</dd></div>
          <div><dt>Не покупать выше</dt><dd>{px(plan.max_entry)}</dd></div>
        </dl>
        <p className={styles.brief}>{reasons.length === 1 && reasons[0] === "ORB_WAITING_BREAKOUT" ? "Ждём роста до цены входа." : reasons.map(orbReason).join(" · ")}</p>
        <details className={styles.details}>
          <summary>Подробнее о плане</summary>
          <dl className={styles.facts}>
            <div><dt>Цены первых 5 минут</dt><dd>{px(plan.range_low)}–{px(plan.range_high)}</dd></div>
            <div><dt>Объём к обычному за 5 минут</dt><dd>{Number(plan.relative_volume).toFixed(2)}×</dd></div>
            <div><dt>Покупка до</dt><dd>{etTime(plan.entry_deadline)} ET</dd></div>
            <div><dt>Закрытие позиции до</dt><dd>{etTime(plan.exit_at)} ET</dd></div>
          </dl>
          <p className={styles.brief}>Цены в плане установлены на день. Перед покупкой проверяем цену и риск ещё раз.</p>
        </details>
        {opp && <>
          <label className={styles.quantity}>Количество акций <input aria-label={`Количество ${plan.symbol}`} type="number" min={1} max={maxQty} value={qty}
            onChange={e=>setQuantities(q=>({...q,[plan.symbol]:Math.max(1,Math.min(maxQty,Math.floor(Number(e.target.value)||1)))}))} /></label>
          <p className={styles.risk}>Риск при максимальной цене входа до стопа: ${((Number(plan.max_entry)-Number(plan.stop))*qty).toFixed(2)} плюс комиссии и проскальзывание. Стоп не гарантирует эту цену.</p>
          <div className={styles.actions}><Button loading={busy === `${opp.id}:approve`} disabled={!buyable || busy !== null || qty < 1} onClick={()=>void decide(opp,"approve",qty)}>{busy === `${opp.id}:approve` ? "Проверяем…" : "Купить"}</Button>{" "}
          <Button variant="ghost" loading={busy === `${opp.id}:skip`} disabled={busy !== null} onClick={()=>void decide(opp,"skip",qty)}>Пропустить</Button></div>
        </>}
      </article>;
    })}
    </div>
  </section>;
}
