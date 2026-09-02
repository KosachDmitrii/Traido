import { useState } from "react";
import type { BuyOpportunity, BuyViability, DeskResponse, EntryWatchCard } from "@/lib/api";
import { decideBuy, decideSell } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";
import {
  flashBuyOk,
  flashHoldOk,
  flashPending,
  flashPendingLocal,
  flashSellOk,
  flashSkipOk,
  humanizeError,
  type FlashMessage,
} from "@/lib/messages";
import type { FlashSlot } from "@/lib/toasts";
import { Button } from "@/ui";

function formatOppQty(opp: BuyOpportunity): string {
  const executed = opp.executed_qty;
  const approved = opp.approved_qty;
  const proposed = opp.proposed_qty ?? opp.risk?.sized_qty;
  if (executed != null && executed !== "") return String(executed);
  if (
    approved != null &&
    approved !== "" &&
    proposed != null &&
    String(approved) !== String(proposed)
  ) {
    return `${proposed}→${approved}`;
  }
  if (approved != null && approved !== "") return String(approved);
  return proposed != null && proposed !== "" ? String(proposed) : "—";
}

/** Whole-share Risk max shown on the card (floor of proposed/sized qty). */
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
  return n.toFixed(3);
}

function waitPrice(w: EntryWatchCard): number {
  const raw = w.last_price ?? w.current_price_at_creation;
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

function waitStatusKey(w: EntryWatchCard): MessageKey {
  const machine = (w.status || "").toLowerCase();
  if (machine === "revalidating") return "rail.wait.machine.revalidating";
  if (machine === "admitted") return "rail.wait.machine.admitted";
  if (machine === "converting") return "rail.wait.machine.converting";
  if (machine === "converted") return "rail.wait.machine.converted";
  const ui = w.ui_state ?? w.status_label;
  if (ui === "APPROACHING") return "rail.wait.status.approaching";
  if (ui === "IN_ZONE" || ui === "TRIGGERED") {
    if (w.buy_blocked) return "rail.wait.status.inZoneBlocked";
    return "rail.wait.status.inZone";
  }
  const price = waitPrice(w);
  const zoneLo = Number(w.entry_zone_low);
  const zoneHi = Number(w.entry_zone_high);
  const target = Number(w.planned_target);
  if (Number.isFinite(target) && price >= target) return "rail.wait.status.passed";
  if (Number.isFinite(zoneLo) && Number.isFinite(zoneHi) && price >= zoneLo && price <= zoneHi) {
    return "rail.wait.status.inZone";
  }
  if (Number.isFinite(zoneHi) && price > zoneHi) return "rail.wait.status.aboveZone";
  return "rail.wait.status.belowZone";
}

type Props = {
  desk: DeskResponse | null;
  scannerLine: string;
  onFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  onRefresh: () => Promise<void>;
};

/** Desk copy for the live-book reading. The card stays; the button changes.
 *
 * Missing viability is treated as unverified: an older payload must not look
 * more buyable than a current one that failed to read the book.
 */
function viabilityView(
  v: BuyViability | undefined,
  t: (key: MessageKey) => string,
): {
  buyable: boolean;
  label: string | null;
  className: string;
} {
  const state = v?.state ?? "unverified";
  if (state === "live" && v?.buyable === true) {
    return { buyable: true, label: null, className: "" };
  }
  if (state === "wide") {
    return { buyable: false, label: t("opp.viability.wide"), className: "opp-viability--blocked" };
  }
  if (state === "drifted") {
    return {
      buyable: false,
      label: t("opp.viability.drifted"),
      className: "opp-viability--blocked",
    };
  }
  if (state === "past_setup") {
    return {
      buyable: false,
      label: t("opp.viability.pastSetup"),
      className: "opp-viability--blocked",
    };
  }
  return {
    buyable: false,
    label: t("opp.viability.unverified"),
    className: "opp-viability--blocked",
  };
}

function buyEnabled(
  opp: BuyOpportunity,
  desk: DeskResponse | null,
  busyId: string | null,
  t: (key: MessageKey) => string,
): boolean {
  if (busyId !== null) return false;
  if (desk?.session?.entries_allowed === false) return false;
  return viabilityView(opp.viability, t).buyable;
}

export function OpportunityRail({ desk, scannerLine, onFlash, onRefresh }: Props) {
  const t = useT();
  const entriesAllowed = desk?.session?.entries_allowed !== false;
  // Actionable BUY cards float above waits/locked proposals so the operator
  // sees what can clear the book right now, not a stack of WAIT plans.
  const buys = [...(desk?.buy_opportunities ?? [])].sort((a, b) => {
    const aOk = entriesAllowed && viabilityView(a.viability, t).buyable ? 0 : 1;
    const bOk = entriesAllowed && viabilityView(b.viability, t).buyable ? 0 : 1;
    return aOk - bOk;
  });
  const waits = desk?.entry_watches ?? [];
  const sells = desk?.sell_opportunities ?? [];
  const [busyId, setBusyId] = useState<string | null>(null);
  /** Operator qty per card; defaults to Risk max until edited. */
  const [qtyById, setQtyById] = useState<Record<string, number>>({});

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
    expectedDecisionVersion?: number,
  ) {
    setBusyId(id);
    let slot: FlashSlot | undefined;
    if (kind === "buy" && act === "approve") {
      slot = onFlash(flashPending(t("toast.pending.sendBuy", { symbol })));
    } else if (kind === "sell" && act === "sell") {
      slot = onFlash(flashPending(t("toast.pending.sendSell", { symbol })));
    } else {
      slot = onFlash(
        flashPendingLocal(t("toast.pending.action", { symbol, act: act.toUpperCase() })),
      );
    }
    try {
      if (kind === "buy") {
        const requestId = crypto.randomUUID();
        const data = await decideBuy(
          id,
          act === "approve" ? "approve" : "skip",
          act === "approve" && qty != null ? qty : undefined,
          act === "approve"
            ? { requestId, expectedDecisionVersion: expectedDecisionVersion ?? 0 }
            : undefined,
        );
        onFlash(
          act === "skip" ? flashSkipOk(symbol) : flashBuyOk(symbol, String(data.status || "")),
          slot,
        );
      } else {
        const data = await decideSell(id, act as "sell" | "hold");
        onFlash(
          act === "hold" ? flashHoldOk(symbol) : flashSellOk(symbol, String(data.status || "")),
          slot,
        );
      }
      await onRefresh();
    } catch (err) {
      onFlash(humanizeError(err instanceof Error ? err.message : String(err)), slot);
      await onRefresh();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <aside className="rail">
      <h2>{t("rail.title")}</h2>
      <p className="stage-note">{t("rail.subtitle")}</p>

      <div className="stage-note" style={{ marginBottom: 12 }}>
        {scannerLine}
      </div>

      {!buys.length && !waits.length && !sells.length ? (
        <div className="block surface">
          <div className="title">{t("rail.empty.title")}</div>
          <div
            className="detail"
            style={{ color: "var(--td-text-muted)", fontFamily: "var(--td-font-sans)" }}
          >
            {t("rail.empty.detail")}
          </div>
        </div>
      ) : null}

      {buys.map((opp) => {
        const c = opp.candidate;
        const busy = busyId === opp.id;
        const viability = viabilityView(opp.viability, t);
        const canBuy = buyEnabled(opp, desk, busyId, t);
        const maxQty = riskMaxQty(opp);
        const chosenQty = qtyFor(opp);
        const statusNote = !entriesAllowed
          ? t("opp.viability.outsideRth")
          : viability.label;
        return (
          <div
            className={`block accent rail-opp${viability.buyable ? "" : " block--waiting"}`}
            key={opp.id}
          >
            <header className="rail-opp__head">
              <div className="rail-opp__title-row">
                <div className="rail-opp__identity">
                  <div className="title">{c.symbol}</div>
                  {c.name ? <div className="rail-opp__name">{c.name}</div> : null}
                </div>
                {c.risk_reward != null ? (
                  <span className="rail-opp__age">{t("rail.buy.rr", { rr: c.risk_reward })}</span>
                ) : null}
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
                <dd className="mono">{fmtPx(c.target)}</dd>
              </div>
              <div>
                <dt>{t("opp.levels.qty")}</dt>
                <dd className="mono">{maxQty ?? formatOppQty(opp)}</dd>
              </div>
            </dl>

            {statusNote ? (
              <p className="opp-card__status opp-viability opp-viability--blocked">{statusNote}</p>
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
                  onClick={() =>
                    onDecide("buy", opp.id, "approve", c.symbol, chosenQty, opp.decision_version)
                  }
                >
                  {busy && busyId === opp.id ? "…" : t("action.buy")}
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
      })}

      {waits.map((w) => {
        const statusKey = waitStatusKey(w);
        return (
          <div
            className={`block accent block--waiting rail-opp rail-wait${
              w.buy_blocked ? " rail-wait--blocked" : ""
            }`}
            key={w.id}
          >
            <header className="rail-opp__head">
              <div className="rail-opp__title-row">
                <div className="rail-opp__identity">
                  <div className="title">{w.symbol}</div>
                  {w.name ? <div className="rail-opp__name">{w.name}</div> : null}
                </div>
                <span className="rail-opp__age">WAIT</span>
              </div>
            </header>

            <dl className="opp-card__levels">
              <div>
                <dt>{t("rail.wait.nowLabel")}</dt>
                <dd className="mono">{fmtPx(w.last_price ?? w.current_price_at_creation)}</dd>
              </div>
              <div>
                <dt>{t("opp.levels.entry")}</dt>
                <dd className="mono">{fmtPx(w.planned_entry)}</dd>
              </div>
              <div>
                <dt>{t("opp.levels.stop")}</dt>
                <dd className="mono">{fmtPx(w.planned_stop)}</dd>
              </div>
              <div>
                <dt>{t("opp.levels.tgt")}</dt>
                <dd className="mono">{fmtPx(w.planned_target)}</dd>
              </div>
            </dl>

            <p
              className={`rail-wait__status${
                statusKey === "rail.wait.status.passed" ? " rail-wait__status--passed" : ""
              }${w.buy_blocked ? " rail-wait__status--blocked" : ""}`}
            >
              {t(statusKey)}
            </p>
          </div>
        );
      })}

      {sells.map((ex) => {
        const p = ex.proposal;
        const busy = busyId === ex.id;
        return (
          <div className="block ink" key={ex.id}>
            <div className="rail-opp__identity">
              <div className="title">{p.symbol}</div>
              {p.name ? <div className="rail-opp__name">{p.name}</div> : null}
            </div>
            <div className="detail">
              {t("rail.sell.header", {
                e: p.entry,
                n: p.current,
                p: p.pnl_pct.toFixed(1),
              })}
            </div>
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
          </div>
        );
      })}
    </aside>
  );
}
