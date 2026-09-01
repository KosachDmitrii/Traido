import { useEffect, useState } from "react";
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
import type { MessageKey } from "@/i18n";
import { Button } from "@/ui";

function riskMaxQty(opp: BuyOpportunity): number | null {
  const raw = opp.proposed_qty ?? opp.risk?.sized_qty;
  if (raw == null || raw === "") return null;
  const n = Math.floor(Number(raw));
  return Number.isFinite(n) && n >= 1 ? n : null;
}

function fmtPx(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return n.toFixed(2);
}

function ageLabel(
  createdAt: string | undefined,
  now: number,
  t: (key: MessageKey, vars?: Record<string, string | number>) => string,
): string | null {
  if (!createdAt) return null;
  const ms = now - new Date(createdAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const min = Math.floor(ms / 60000);
  if (min < 1) return t("rail.age.justNow");
  if (min < 60) return t("rail.age.minutes", { n: min });
  return t("rail.age.hours", { n: Math.floor(min / 60) });
}

function viabilityView(
  opp: BuyOpportunity,
  entriesAllowed: boolean,
  t: ReturnType<typeof useT>,
): { buyable: boolean; label: string | null } {
  if (!entriesAllowed) {
    return { buyable: false, label: t("opp.viability.outsideRth") };
  }
  const state = opp.viability?.state ?? "unverified";
  if (state === "live" && opp.viability?.buyable === true) {
    return { buyable: true, label: null };
  }
  if (state === "wide") return { buyable: false, label: t("opp.viability.wide") };
  if (state === "drifted") return { buyable: false, label: t("opp.viability.drifted") };
  if (state === "past_setup") return { buyable: false, label: t("opp.viability.pastSetup") };
  return { buyable: false, label: t("opp.viability.unverified") };
}

export function OpportunitiesPage() {
  const t = useT();
  const { desk, showFlash, refreshAll } = useDesk();
  const buys = desk?.buy_opportunities ?? [];
  const sells = desk?.sell_opportunities ?? [];
  const [busyId, setBusyId] = useState<string | null>(null);
  const [qtyById, setQtyById] = useState<Record<string, number>>({});
  const [now, setNow] = useState(() => Date.now());
  const entriesAllowed = desk?.session?.entries_allowed !== false;

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(tick);
  }, []);

  function qtyFor(opp: BuyOpportunity): number | null {
    const max = riskMaxQty(opp);
    if (max == null) return null;
    const chosen = qtyById[opp.id];
    if (chosen == null || !Number.isFinite(chosen)) return max;
    return Math.min(max, Math.max(1, Math.floor(chosen)));
  }

  function setQty(opp: BuyOpportunity, next: number) {
    const max = riskMaxQty(opp);
    if (max == null) return;
    const n = Number.isFinite(next) ? Math.floor(next) : max;
    setQtyById((prev) => ({ ...prev, [opp.id]: Math.min(max, Math.max(1, n)) }));
  }

  async function onDecide(
    kind: "buy" | "sell",
    id: string,
    act: string,
    symbol: string,
    qty?: number | null,
  ) {
    setBusyId(id);
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
        const data = await decideBuy(
          id,
          act === "approve" ? "approve" : "skip",
          act === "approve" && qty != null ? qty : undefined,
        );
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
            const busy = busyId === opp.id;
            const age = ageLabel(opp.created_at, now, t);
            const viability = viabilityView(opp, entriesAllowed, t);
            const canBuy = viability.buyable && busyId === null;
            const maxQty = riskMaxQty(opp);
            const chosenQty = qtyFor(opp);
            const statusNote = viability.label;
            return (
              <div
                className={`block accent rail-opp${viability.buyable ? "" : " block--waiting"}`}
                key={opp.id}
              >
                <header className="rail-opp__head">
                  <div className="rail-opp__title-row">
                    <div className="title">{c.symbol}</div>
                    {age ? <span className="rail-opp__age">{age}</span> : null}
                  </div>
                  <div className="rail-opp__sub">
                    {(c.thesis || "bullish").toUpperCase()}
                    {" · "}
                    {t("rail.buy.quality", { q: c.entry_quality ?? "—" })}
                    {" · "}
                    {t("rail.buy.conf", { conf: ((c.confidence || 0) * 100).toFixed(0) })}
                    {" · "}
                    {t("rail.buy.rr", { rr: c.risk_reward })}
                  </div>
                </header>

                <dl className="opp-card__levels">
                  <div>
                    <dt>{t("opp.levels.entry")}</dt>
                    <dd className="mono">{fmtPx(c.entry)}</dd>
                  </div>
                  <div>
                    <dt>{t("opp.levels.stop")}</dt>
                    <dd className="mono">{fmtPx(c.stop)}</dd>
                  </div>
                  <div>
                    <dt>{t("opp.levels.tgt")}</dt>
                    <dd className="mono">
                      {fmtPx(c.target)}
                      {c.target_reachability ? (
                        <span className="rail-opp__reach"> {c.target_reachability}</span>
                      ) : null}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("opp.levels.qty")}</dt>
                    <dd className="mono">{maxQty ?? "—"}</dd>
                  </div>
                </dl>

                {statusNote ? (
                  <p className="opp-card__status opp-viability opp-viability--blocked">
                    {statusNote}
                  </p>
                ) : null}

                <footer className="rail-opp__footer">
                  {maxQty != null && chosenQty != null ? (
                    <label className="opp-qty">
                      <span className="opp-qty__label">{t("opp.qty.label")}</span>
                      <input
                        className="opp-qty__input mono"
                        type="number"
                        inputMode="numeric"
                        min={1}
                        max={maxQty}
                        step={1}
                        value={chosenQty}
                        disabled={busyId !== null}
                        onChange={(e) => setQty(opp, Number(e.target.value))}
                      />
                      <span className="opp-qty__max">{t("opp.qty.max", { n: maxQty })}</span>
                    </label>
                  ) : (
                    <span />
                  )}
                  <div className="actions">
                    <Button
                      variant="accent"
                      disabled={!canBuy}
                      title={
                        canBuy
                          ? undefined
                          : statusNote ||
                            (!entriesAllowed
                              ? t("opp.buy.title.outsideRth")
                              : t("opp.buy.title.locked"))
                      }
                      onClick={() => onDecide("buy", opp.id, "approve", c.symbol, chosenQty)}
                    >
                      {busy ? "…" : t("action.buy")}
                    </Button>
                    <Button
                      variant="light"
                      disabled={busyId !== null}
                      onClick={() => onDecide("buy", opp.id, "skip", c.symbol)}
                    >
                      {t("action.skip")}
                    </Button>
                  </div>
                </footer>
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
              <div className="block ink rail-opp" key={ex.id}>
                <header className="rail-opp__head">
                  <div className="rail-opp__title-row">
                    <div className="title">{p.symbol}</div>
                    <span className="rail-opp__age">
                      {t("opportunities.sell.pnl", { n: p.pnl_pct.toFixed(1) })}
                    </span>
                  </div>
                </header>
                <dl className="opp-card__levels opp-card__levels--sell">
                  <div>
                    <dt>{t("opp.levels.entry")}</dt>
                    <dd className="mono">{fmtPx(p.entry)}</dd>
                  </div>
                  <div>
                    <dt>{t("opportunities.sell.now")}</dt>
                    <dd className="mono">{fmtPx(p.current)}</dd>
                  </div>
                </dl>
                {(p.reasons || []).length > 0 ? (
                  <p className="opp-card__reasons">{(p.reasons || []).join(" · ")}</p>
                ) : null}
                <footer className="rail-opp__footer">
                  <span />
                  <div className="actions">
                    <Button
                      variant="ink"
                      disabled={busyId !== null}
                      onClick={() => onDecide("sell", ex.id, "sell", p.symbol)}
                    >
                      {busy ? "…" : t("action.sell")}
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={busyId !== null}
                      onClick={() => onDecide("sell", ex.id, "hold", p.symbol)}
                    >
                      {t("action.hold")}
                    </Button>
                  </div>
                </footer>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
