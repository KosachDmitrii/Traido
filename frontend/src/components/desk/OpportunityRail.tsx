import { ArrowUpRight, Layers, Radar, ShieldCheck } from "lucide-react";
import styles from "./OpportunityRail.module.css";
import { useState } from "react";
import type { DeskResponse, BuyOpportunity } from "@/lib/api";
import { decideBuy } from "@/lib/api";
import { humanizeError, type FlashMessage } from "@/lib/messages";
import type { FlashSlot } from "@/lib/toasts";
import { Button, LoadingDots } from "@/ui";
import { CurrentPrice } from "./CurrentPrice";
import { autoBuyPresentation } from "./autoBuyPresentation";
import { orbReason, px, etTime } from "./orbLabels";
import { isPotentialOrbState, orbSignalStatus, observationTime } from "./orbSignalStatus";

type Props = { desk: DeskResponse | null; onFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  onRefresh: () => Promise<void>; layout?: "rail" | "page" };

export function OpportunityRail({ desk, onFlash, onRefresh, layout = "rail" }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const buys = desk?.buy_opportunities ?? [];
  const lightAvailable = desk?.light_available === true;
  const automatic = desk?.auto_trigger?.enabled === true;
  const manualTarget = desk?.position_exit_policy === "manual_target";
  const orbVersions = ["orb@1.1.0", "orb@1.2.0", "orb@1.3.0", "orb@1.4.0", "orb@1.5.0", "orb@2.0.0", "orb@2.1.0", "orb@2.2.0"];
  const planPriority = (symbol: string) => {
    const opp = buys.find(o => o.candidate.symbol === symbol && orbVersions.includes(o.candidate.strategy_version ?? ""));
    if (!opp) return 2;
    const qty = Math.floor(Number(opp.proposed_qty ?? opp.risk?.sized_qty ?? 0));
    return opp.viability?.buyable === true && desk?.session?.entries_allowed !== false && qty > 0 ? 0 : 1;
  };
  const plans = Object.values(desk?.orb?.plans ?? {}).filter(plan => {
    const state = desk?.orb?.states?.[plan.symbol];
    const opp = buys.find(o => o.candidate.symbol === plan.symbol && orbVersions.includes(o.candidate.strategy_version ?? ""));
    const execution = desk?.orb?.execution?.[plan.symbol];
    if (desk?.orb?.execution?.[plan.symbol]?.stage === "CLOSED") return false;
    return isPotentialOrbState(state?.state, state?.reasons ?? [], !!opp, execution?.stage);
  }).sort((a, b) => planPriority(a.symbol) - planPriority(b.symbol));
  async function decide(opp: BuyOpportunity, decision: "approve" | "skip", qty: number) {
    setBusy(`${opp.id}:${decision}`);
    try {
      const result = await decideBuy(opp.id, decision, qty, { expectedDecisionVersion: opp.decision_version ?? 0 });
      onFlash({ kind: "ok", title: decision === "skip" ? "Предложение пропущено" : "Ответ на запрос покупки",
        detail: decision === "skip" ? "Сейчас не покупаем. План снова предложит вход после нового движения цены." : `Статус: ${result.status ?? "получен"}. Исполнение проверяется у брокера.` });
    } catch (error) {
      onFlash({ kind: "error", title: "Запрос не выполнен", detail: humanizeError(error instanceof Error ? error.message : String(error)).detail });
    } finally { setBusy(null); await onRefresh(); }
  }
  return <section className={layout === "page" ? styles.page : styles.rail}>
    {layout === "page" ? <>
      <header className={styles.heading}><div><span className={styles.eyebrow}>ORB · ALPACA PAPER</span><h1>Потенциальные покупки</h1><p>Только инструменты, которые уже приблизились к реальному входу.</p></div><span className={styles.mode}><ShieldCheck size={15} />{automatic ? "Автопокупка" : "Ручное подтверждение"}</span></header>
      <div className={styles.summary}>
        <div><Layers size={18} /><span>Потенциальные входы</span><strong>{lightAvailable ? plans.length : "—"}</strong></div>
        <div><ArrowUpRight size={18} /><span>Готовы к покупке</span><strong>{lightAvailable ? buys.filter(o => orbVersions.includes(o.candidate.strategy_version ?? "")).length : "—"}</strong></div>
      </div>
      <div className={styles.sectionHead}><h2>Кандидаты на вход</h2><span>История проверяется от 09:35 ET</span></div>
    </> : <>

    <header className={styles.railHead}><span className={styles.eyebrow}>ALPACA PAPER</span><h2>Потенциальные покупки</h2><p>Только кандидаты после пробоя</p></header>
    </>}
    {plans.length > 0 && desk?.orb?.reason && <p className={styles.notice} role="alert">{orbReason(desk.orb.reason)}</p>}
    {!plans.length && <div className={styles.empty} role="status"><span className={styles.emptyIcon}><Radar size={28} /></span><h3>{!lightAvailable ? "Получаем торговые данные" : "Потенциальных покупок пока нет"}</h3><p>{!lightAvailable ? "Загружаем состояние торговой сессии." : "Карточка появится после подтверждённого пробоя и будет скрыта, если данные неполные или условия нарушены."}</p></div>}
    <div className={styles.grid}>
    {plans.map(plan => {
      const state = desk?.orb?.states?.[plan.symbol];
      const retest = plan.evidence?.retest;
      const isRetest = ["orb@2.0.0", "orb@2.1.0", "orb@2.2.0"].includes(plan.version ?? "");
      const ready = !isRetest || retest?.phase === "ready";
      const opp = buys.find(o => o.candidate.symbol === plan.symbol && orbVersions.includes(o.candidate.strategy_version ?? ""));
      const maxQty = Math.max(0, Math.floor(Number(opp?.proposed_qty ?? opp?.risk?.sized_qty ?? 0)));
      const qty = Math.min(maxQty, quantities[plan.symbol] ?? maxQty);
      const buyable = state?.state !== "DATA_BLOCKED" && !!opp && opp.viability?.buyable === true && desk?.session?.entries_allowed !== false && maxQty > 0;
      const reasons = opp && !opp.viability?.buyable ? opp.viability?.reasons ?? ["ORB_QUOTE_STALE"] : state?.reasons ?? [isRetest ? "ORB_RETEST_WAIT_BREAKOUT" : plan.version === "orb@1.5.0" ? "ORB_WAITING_PULLBACK" : "ORB_WAITING_BREAKOUT"];
      const ask = state?.state === "DATA_BLOCKED" ? undefined : opp?.viability?.measured?.ask ?? state?.ask;
      const execution = desk?.orb?.execution?.[plan.symbol];
      const auto = autoBuyPresentation(execution, state?.state, desk?.auto_trigger?.available !== false);
      const signalStatus = orbSignalStatus(state?.state, reasons);
      const observedTime = observationTime(state?.observed_at);
      return <article key={plan.symbol} className={`opp-card ${styles.card}`} data-buyable={automatic ? auto.tone === "active" : buyable}>
        <div className={styles.cardHead}><div className={styles.identity}><div><h3>{plan.symbol}</h3><span className={styles.strategy}>{isRetest ? "ORB · Возврат" : "ORB · Покупка"}</span></div></div><strong className={styles.status}>{automatic ? auto.title : buyable ? "Можно купить" : signalStatus}</strong></div>
        {(plan.name || opp?.candidate.name) && <p className={styles.companyName}>{plan.name || opp?.candidate.name}</p>}
        <dl className={styles.prices}>
          <div><dt>Ask</dt><dd><CurrentPrice value={ask} /></dd></div>
          <div><dt>{ready ? "Зона покупки" : "Уровень ORB"}</dt><dd>{ready && isRetest ? `${px(plan.trigger)}–${px(plan.max_entry)}` : px(plan.trigger)}</dd></div>
          {ready && <div><dt>{manualTarget ? "Уровень риска" : "Стоп"}</dt><dd>{px(plan.stop)}</dd></div>}
          {ready && isRetest && <div><dt>Цель</dt><dd>{px(retest?.target)}</dd></div>}
        </dl>
        {!ready && <p className={styles.brief}>{reasons.map(orbReason).join(" · ")}</p>}
        {observedTime && <small className={styles.checkedAt}>Проверено {observedTime} ET</small>}
        {automatic && <div className={styles.autoStatus} data-tone={auto.tone} role="status" aria-live="polite">
          <div className={styles.autoTitle}>{auto.loading && <LoadingDots ariaLabel="Обработка покупки" />}<strong>{auto.title}</strong></div>
          <p>{auto.detail}</p>
          {auto.lastError && <p>Последняя попытка: {auto.lastError}</p>}
          {execution?.retry_at && !auto.loading && !auto.terminal && <small>Повторная проверка — не раньше {etTime(execution.retry_at)} ET, по очереди.</small>}
          {!!execution?.attempts && !auto.terminal && <small>Попыток проверки: {execution.attempts}</small>}
        </div>}
        {opp && (!automatic || !auto.terminal) && <>
          <label className={styles.quantity}>Количество акций <input aria-label={`Количество ${plan.symbol}`} type="number" min={1} max={maxQty} readOnly={automatic} value={automatic ? maxQty : qty}
            onChange={e=>setQuantities(q=>({...q,[plan.symbol]:Math.max(1,Math.min(maxQty,Math.floor(Number(e.target.value)||1)))}))} /></label>
          <p className={styles.risk}>{manualTarget ? "Расчётное расстояние до уровня риска" : "Риск при максимальной цене входа до стопа"}: ${((Number(plan.max_entry)-Number(plan.stop))*(automatic ? maxQty : qty)).toFixed(2)} плюс комиссии и проскальзывание. {manualTarget ? "Автостоп отключён; фактический убыток может быть больше." : "Стоп не гарантирует эту цену."}</p>
          <div className={styles.actions} data-auto={automatic}>{!automatic && <Button loading={busy === `${opp.id}:approve`} disabled={!buyable || busy !== null || qty < 1} onClick={()=>void decide(opp,"approve",qty)}>{busy === `${opp.id}:approve` ? "Проверяем…" : "Купить"}</Button>}
          <Button variant="ghost" loading={busy === `${opp.id}:skip`} disabled={busy !== null || (automatic && auto.loading)} onClick={()=>void decide(opp,"skip",qty)}>Пропустить</Button></div>
        </>}
      </article>;
    })}
    </div>
  </section>;
}
