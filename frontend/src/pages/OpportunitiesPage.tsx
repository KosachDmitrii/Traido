import { useState } from "react";
import type { BuyOpportunity } from "@/lib/api";
import { decideBuy, decideSell } from "@/lib/api";
import {
  flashBuyOk,
  flashHoldOk,
  flashPending,
  flashPendingLocal,
  flashSellOk,
  flashSkipOk,
  humanizeError,
} from "@/lib/messages";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";

function buyable(opp: BuyOpportunity, entriesAllowed: boolean, busy: boolean): boolean {
  if (busy || !entriesAllowed) return false;
  return opp.viability?.buyable === true && opp.viability.state === "live";
}

function viabilityLabel(
  opp: BuyOpportunity,
  entriesAllowed: boolean,
  t: ReturnType<typeof useT>,
): string | null {
  if (!entriesAllowed) return t("opp.viability.outsideRth");
  const state = opp.viability?.state ?? "unverified";
  if (state === "live" && opp.viability?.buyable) return null;
  if (state === "wide") return t("opp.viability.wide");
  if (state === "drifted") return t("opp.viability.drifted");
  if (state === "past_setup") return t("opp.viability.pastSetup");
  return t("opp.viability.unverified");
}

export function OpportunitiesPage() {
  const t = useT();
  const { desk, showFlash, refreshAll } = useDesk();
  const buys = desk?.buy_opportunities ?? [];
  const sells = desk?.sell_opportunities ?? [];
  const [busyId, setBusyId] = useState<string | null>(null);
  const entriesAllowed = desk?.session?.entries_allowed !== false;

  async function onDecide(kind: "buy" | "sell", id: string, act: string, symbol: string) {
    setBusyId(id);
    // The result replaces the pending message in place instead of stacking a
    // second card next to it — two states of one request, not two events.
    const reachesBroker = act === "approve" || act === "sell";
    let slot;
    if (reachesBroker) {
      slot = showFlash(
        flashPending(
          act === "approve"
            ? t("toast.pending.sendBuy", { symbol })
            : t("toast.pending.sendSell", { symbol }),
        ),
      );
    } else {
      slot = showFlash(
        flashPendingLocal(t("toast.pending.action", { symbol, act: act.toUpperCase() })),
      );
    }
    try {
      if (kind === "buy") {
        const data = await decideBuy(id, act === "approve" ? "approve" : "skip");
        showFlash(
          act === "skip" ? flashSkipOk(symbol) : flashBuyOk(symbol, String(data.status || "")),
          slot,
        );
      } else {
        const data = await decideSell(id, act as "sell" | "hold");
        showFlash(
          act === "hold" ? flashHoldOk(symbol) : flashSellOk(symbol, String(data.status || "")),
          slot,
        );
      }
      await refreshAll();
    } catch (err) {
      showFlash(humanizeError(err instanceof Error ? err.message : String(err)), slot);
      await refreshAll();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="card page-card">
      <h3 className="page-section-title">{t("opportunities.buysTitle", { n: buys.length })}</h3>
      <div className="opp-grid">
        {buys.length === 0 ? (
          <div className="empty-hint">{t("opportunities.empty.buys")}</div>
        ) : (
          buys.map((opp) => {
            const c = opp.candidate;
            const canBuy = buyable(opp, entriesAllowed, busyId !== null);
            const note = viabilityLabel(opp, entriesAllowed, t);
            return (
              <div className="opp-card" key={opp.id}>
                <div className="opp-card__head">
                  <strong>{c.symbol}</strong>
                  <span>
                    {t("opportunities.buy.meta", {
                      conf: (c.confidence * 100).toFixed(0),
                      rr: c.risk_reward,
                    })}
                  </span>
                </div>
                <div className="opp-card__meta mono">
                  {t("opp.levels.entry")} {c.entry} · {t("opp.levels.stop")} {c.stop} ·{" "}
                  {t("opp.levels.tgt")} {c.target}
                  {opp.risk?.sized_qty ? ` · ${t("opp.levels.qty")} ${opp.risk.sized_qty}` : ""}
                </div>
                {note ? <div className="opp-viability opp-viability--blocked">{note}</div> : null}
                <div className="opp-card__actions">
                  <button
                    type="button"
                    className="btn-ink"
                    disabled={!canBuy}
                    onClick={() => onDecide("buy", opp.id, "approve", c.symbol)}
                  >
                    {t("action.buy")}
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busyId !== null}
                    onClick={() => onDecide("buy", opp.id, "skip", c.symbol)}
                  >
                    {t("action.skip")}
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>

      <h3 className="page-section-title">{t("opportunities.sellsTitle", { n: sells.length })}</h3>
      <div className="opp-grid">
        {sells.length === 0 ? (
          <div className="empty-hint">{t("opportunities.empty.sells")}</div>
        ) : (
          sells.map((ex) => {
            const p = ex.proposal;
            const busy = busyId === ex.id;
            return (
              <div className="opp-card" key={ex.id}>
                <div className="opp-card__head">
                  <strong>{p.symbol}</strong>
                  <span>{t("opportunities.sell.pnl", { n: p.pnl_pct.toFixed(1) })}</span>
                </div>
                <div className="opp-card__meta mono">
                  {t("opportunities.sell.entryNow")} {p.entry} · {p.current}
                </div>
                <div className="opp-card__actions">
                  <button
                    type="button"
                    className="btn-ink"
                    disabled={busy}
                    onClick={() => onDecide("sell", ex.id, "sell", p.symbol)}
                  >
                    {t("action.sell")}
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => onDecide("sell", ex.id, "hold", p.symbol)}
                  >
                    {t("action.hold")}
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
