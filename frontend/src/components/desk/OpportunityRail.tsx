import { useMemo, useState } from "react";
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
import {
  watchApproaching,
  watchAboveStrictZone,
  watchBelowStrictZone,
  watchDistanceTie,
  watchInOrNearZone,
  watchInTriggerBand,
  watchPipelineSortRank,
  watchShowsTriggerBand,
  waitPrice,
} from "@/lib/watchGeometry";
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

const WAIT_TRIGGER_CODES = new Set([
  "PRICE_ENTERS_ZONE",
  "ZONE_RECLAIM",
  "VWAP_HOLDS",
  "MOMENTUM_TURNS_POSITIVE",
  "PULLBACK_VOL_DIGESTING",
  "MARKET_ALIGNMENT_VALID",
]);

/** Stale geometry at cushion fill — not live quote gates like spread. */
const CUSHION_SUPPRESSED_HINTS = new Set([
  "EXTREME_CHASE",
  "INVALID_STOP",
  "TARGET_UNREALISTIC",
  "ATR_ONLY_STOP",
]);

const SPREAD_HINT_CODES = new Set([
  "SPREAD_ACCEPTABLE",
  "SPREAD_TOO_WIDE",
  "EXTREME_SPREAD",
]);

function filterResolvedSpreadHints(w: EntryWatchCard, hint: string | null): string | null {
  if (!hint || w.spread_acceptable !== true) return hint;
  const codes = hint
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean)
    .filter((c) => !SPREAD_HINT_CODES.has(c.split(":")[0] ?? c));
  return codes.length > 0 ? codes.join(", ") : null;
}

function filterHintForCushion(w: EntryWatchCard, hint: string | null): string | null {
  if (!hint || !watchInTriggerBand(w)) return hint;
  const codes = hint
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean)
    .filter((c) => !CUSHION_SUPPRESSED_HINTS.has(c.split(":")[0] ?? c));
  return codes.length > 0 ? codes.join(", ") : null;
}

function watchRevalidationHint(w: EntryWatchCard): string | null {
  const price = waitPrice(w);
  const trigHi = Number(w.entry_zone_trigger_high ?? w.entry_zone_high);
  const trigLo = Number(w.entry_zone_trigger_low ?? w.entry_zone_low);
  if (Number.isFinite(trigHi) && price > trigHi) return null;
  if (Number.isFinite(trigLo) && price < trigLo) return null;

  const machine = (w.status || "").toLowerCase();
  if (machine === "waiting" && !watchInTriggerBand(w)) {
    return null;
  }
  if (w.desk_revalidation_hint) {
    const codes = w.desk_revalidation_hint.split(",").map((c) => c.trim());
    const actionable = codes.filter((c) => !WAIT_TRIGGER_CODES.has(c.split(":")[0] ?? c));
    if (actionable.length === 0) return null;
    return filterResolvedSpreadHints(w, filterHintForCushion(w, actionable.join(", ")));
  }
  for (let i = (w.reasons?.length ?? 0) - 1; i >= 0; i--) {
    const raw = w.reasons?.[i];
    if (!raw) continue;
    if (raw.startsWith("TRIGGERED_CONDITIONS_PENDING:")) {
      const codes = (raw.split(":", 2)[1] ?? "")
        .split(",")
        .map((c) => c.trim())
        .filter(Boolean);
      const actionable = codes.filter((c) => !WAIT_TRIGGER_CODES.has(c.split(":")[0] ?? c));
      if (actionable.length === 0) return null;
      return filterResolvedSpreadHints(w, filterHintForCushion(w, actionable.join(", ")));
    }
    if (raw.includes(",")) {
      const codes = raw
        .split(",")
        .map((c) => c.trim())
        .filter(Boolean);
      const actionable = codes.filter((c) => !WAIT_TRIGGER_CODES.has(c.split(":")[0] ?? c));
      if (actionable.length > 0) {
        return filterResolvedSpreadHints(w, filterHintForCushion(w, actionable.join(", ")));
      }
    }
    if (
      raw.startsWith("INSUFFICIENT_EFFECTIVE_RR:") ||
      raw === "EXTREME_CHASE" ||
      raw === "INVALID_STOP" ||
      raw === "TARGET_UNREALISTIC" ||
      raw === "EXTREME_SPREAD" ||
      raw === "SPREAD_TOO_WIDE" ||
      raw === "ZONE_ARRIVAL_MISSING" ||
      raw === "INSUFFICIENT_BARS" ||
      raw === "STALE_DATA" ||
      raw === "STALE_BARS" ||
      raw === "MARKET_DATA_UNHEALTHY" ||
      raw === "DATA_BLOCKED" ||
      raw === "SETUP_QUALITY_BELOW_THRESHOLD" ||
      raw === "ENTRY_QUALITY_BELOW_THRESHOLD"
    ) {
      return filterResolvedSpreadHints(w, filterHintForCushion(w, raw));
    }
  }
  return null;
}

function deskBlockReasonLabel(
  t: (key: MessageKey, vars?: Record<string, string | number>) => string,
  reason: string | null | undefined,
  w?: EntryWatchCard,
  entriesAllowed?: boolean,
): string | null {
  if (!reason) return null;
  const first = reason.split(",")[0]?.trim() ?? reason;
  const colon = first.indexOf(":");
  const code = colon >= 0 ? first.slice(0, colon) : first;
  const detail = colon >= 0 ? first.slice(colon + 1) : undefined;
  if (code === "DATA_BLOCKED") {
    const next = reason
      .split(",")
      .map((c) => c.trim())
      .find((c) => c && c !== "DATA_BLOCKED");
    if (next) return deskBlockReasonLabel(t, next, w, entriesAllowed);
    return t("rail.wait.block.DATA_BLOCKED");
  }
  if (
    (code === "STALE_DATA" ||
      code === "STALE_BARS" ||
      code === "MARKET_DATA_UNHEALTHY" ||
      code === "QUOTE_TIMESTAMP_INVALID" ||
      code === "BAR_TIMESTAMP_MISSING") &&
    entriesAllowed === false
  ) {
    return t("rail.wait.block.STALE_DATA_AFTER_HOURS");
  }
  if (
    (code === "SPREAD_ACCEPTABLE" || code === "EXTREME_SPREAD" || code === "SPREAD_TOO_WIDE") &&
    w?.spread_acceptable === true
  ) {
    return null;
  }
  if (
    (code === "SPREAD_ACCEPTABLE" || code === "EXTREME_SPREAD") &&
    w?.live_spread_bps != null &&
    w?.max_spread_bps != null
  ) {
    return t("rail.wait.block.SPREAD_DETAIL", {
      bps: w.live_spread_bps,
      max: w.max_spread_bps,
    });
  }
  const key = `rail.wait.block.${code}` as MessageKey;
  const translated = t(key);
  if (translated !== key) {
    return detail && translated.includes("{detail}")
      ? translated.replace("{detail}", detail)
      : translated;
  }
  return reason;
}

function waitBadgeKey(w: EntryWatchCard): MessageKey {
  const machine = (w.status || "").toLowerCase();
  if (machine === "triggered") return "rail.wait.badge.triggered";
  if (machine === "revalidating") return "rail.wait.badge.revalidating";
  if (machine === "admitted") return "rail.wait.badge.admitted";
  if (machine === "converting") return "rail.wait.badge.converting";
  return "rail.wait.badge";
}

function waitStatusKey(w: EntryWatchCard): MessageKey {
  const machine = (w.status || "").toLowerCase();
  const price = waitPrice(w);
  const zoneLo = Number(w.entry_zone_low);
  const zoneHi = Number(w.entry_zone_high);
  const target = Number(w.planned_target);

  // Same geometry as SessionDecisionStrip (backend ui_state + ATR distance).
  if (watchInOrNearZone(w)) {
    const aboveCushion = watchAboveStrictZone(w);
    const belowCushion = watchBelowStrictZone(w);
    if (w.buy_blocked) {
      if (aboveCushion) return "rail.wait.status.aboveZoneCushionBlocked";
      if (belowCushion) return "rail.wait.status.belowZoneCushionBlocked";
      return "rail.wait.status.inZoneBlocked";
    }
    if (machine === "revalidating") {
      if (aboveCushion) return "rail.wait.status.aboveZoneCushion";
      if (belowCushion) return "rail.wait.status.belowZoneCushion";
      return "rail.wait.machine.revalidating";
    }
    if (machine === "triggered") return "rail.wait.machine.triggered";
    if (machine === "admitted") return "rail.wait.machine.admitted";
    if (machine === "converting") return "rail.wait.machine.converting";
    if (machine === "converted") return "rail.wait.machine.converted";
    if (aboveCushion) return "rail.wait.status.aboveZoneCushion";
    if (belowCushion) return "rail.wait.status.belowZoneCushion";
    return "rail.wait.status.inZone";
  }

  if (machine === "revalidating") return "rail.wait.machine.revalidating";
  if (machine === "triggered") return "rail.wait.machine.triggered";
  if (machine === "admitted") return "rail.wait.machine.admitted";
  if (machine === "converting") return "rail.wait.machine.converting";
  if (machine === "converted") return "rail.wait.machine.converted";

  if (Number.isFinite(zoneHi) && price > zoneHi) {
    if (watchInTriggerBand(w)) return "rail.wait.status.aboveZoneCushion";
    return "rail.wait.status.aboveZone";
  }
  if (Number.isFinite(zoneLo) && price < zoneLo) {
    if (watchInTriggerBand(w)) return "rail.wait.status.belowZoneCushion";
    return "rail.wait.status.belowZone";
  }

  if (watchApproaching(w)) return "rail.wait.status.approaching";

  if (Number.isFinite(target) && price >= target) return "rail.wait.status.passed";
  return "rail.wait.status.belowZone";
}

type Props = {
  desk: DeskResponse | null;
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

type RailProposal =
  | { kind: "buy"; opp: BuyOpportunity }
  | { kind: "wait"; watch: EntryWatchCard };

function sortRailProposals(
  buys: BuyOpportunity[],
  waits: EntryWatchCard[],
  entriesAllowed: boolean,
  t: (key: MessageKey) => string,
): RailProposal[] {
  const buyTie = (opp: BuyOpportunity) =>
    entriesAllowed && viabilityView(opp.viability, t).buyable ? 0 : 1;

  return [
    ...buys.map((opp) => ({
      proposal: { kind: "buy" as const, opp },
      rank: 0,
      tie: buyTie(opp),
    })),
    ...waits.map((watch) => ({
      proposal: { kind: "wait" as const, watch },
      rank: watchPipelineSortRank(watch),
      tie: watchDistanceTie(watch),
    })),
  ]
    .sort((a, b) => a.rank - b.rank || a.tie - b.tie)
    .map(({ proposal }) => proposal);
}

export function OpportunityRail({ desk, onFlash, onRefresh }: Props) {
  const t = useT();
  const entriesAllowed = desk?.session?.entries_allowed !== false;
  const buys = desk?.buy_opportunities ?? [];
  const waits = desk?.entry_watches ?? [];
  const proposals = useMemo(
    () => sortRailProposals(buys, waits, entriesAllowed, t),
    [buys, waits, entriesAllowed, t],
  );
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

      {!proposals.length && !sells.length ? (
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

      {proposals.map((item) => {
        if (item.kind === "buy") {
          const opp = item.opp;
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
        }

        const w = item.watch;
        const statusKey = waitStatusKey(w);
        const inZone = watchInOrNearZone(w);
        const approaching = watchApproaching(w);
        return (
          <div
            className={`block accent block--waiting rail-opp rail-wait${
              inZone ? " rail-wait--in-zone" : approaching ? " rail-wait--approaching" : ""
            }${w.buy_blocked ? " rail-wait--blocked" : ""}`}
            key={w.id}
          >
            <header className="rail-opp__head">
              <div className="rail-opp__title-row">
                <div className="rail-opp__identity">
                  <div className="title">{w.symbol}</div>
                  {w.name ? <div className="rail-opp__name">{w.name}</div> : null}
                </div>
                <span className="rail-opp__age">{t(waitBadgeKey(w))}</span>
              </div>
            </header>

            <dl className="opp-card__levels">
              <div>
                <dt>{t("rail.wait.nowLabel")}</dt>
                <dd className="mono rail-wait__now">
                  {fmtPx(w.last_price ?? w.current_price_at_creation)}
                  {w.price_tick === "up" ? (
                    <span className="pos-pnl pos-pnl--up rail-wait__tick" aria-hidden>
                      <span className="pos-pnl__arrow">↑</span>
                    </span>
                  ) : null}
                  {w.price_tick === "down" ? (
                    <span className="pos-pnl pos-pnl--down rail-wait__tick" aria-hidden>
                      <span className="pos-pnl__arrow">↓</span>
                    </span>
                  ) : null}
                </dd>
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

            <p className="rail-wait__zone mono">
              {t("rail.wait.zone", {
                lo: fmtPx(w.entry_zone_low),
                hi: fmtPx(w.entry_zone_high),
              })}
            </p>
            {watchShowsTriggerBand(w) ? (
              <p className="rail-wait__zone rail-wait__zone--trigger mono">
                {t("rail.wait.zoneTrigger", {
                  lo: fmtPx(w.entry_zone_trigger_low),
                  hi: fmtPx(w.entry_zone_trigger_high),
                })}
              </p>
            ) : null}

            <p
              className={`rail-wait__status${
                statusKey === "rail.wait.status.passed" ? " rail-wait__status--passed" : ""
              }${w.buy_blocked ? " rail-wait__status--blocked" : ""}`}
            >
              {t(statusKey)}
            </p>
            {w.buy_blocked && w.desk_block_reason ? (
              <p className="rail-wait__block-reason">
                {deskBlockReasonLabel(t, w.desk_block_reason, w, entriesAllowed)}
              </p>
            ) : null}
            {!w.buy_blocked && watchRevalidationHint(w) ? (
              <p className="rail-wait__block-reason">
                {deskBlockReasonLabel(t, watchRevalidationHint(w), w, entriesAllowed)}
              </p>
            ) : null}
            {w.buy_blocked && !w.desk_block_reason && w.zone_arrival_quality != null ? (
              <p className="rail-wait__block-reason">
                {t("rail.wait.inZoneBlockedNote")} · {w.zone_arrival_quality}/100
                {w.zone_arrival_type ? ` · ${w.zone_arrival_type}` : ""}
              </p>
            ) : null}
            {!w.buy_blocked && inZone && (w.status || "").toLowerCase() === "waiting" ? (
              <p className="rail-wait__block-reason rail-wait__block-reason--muted">
                {t("rail.wait.inZonePendingNote")}
              </p>
            ) : null}
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
