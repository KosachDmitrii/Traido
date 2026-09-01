import { useEffect, useState } from "react";
import type { BuyOpportunity, BuyViability, DeskResponse } from "@/lib/api";
import { decideBuy, decideSell } from "@/lib/api";
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

type Props = {
  desk: DeskResponse | null;
  scannerLine: string;
  onFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  onRefresh: () => Promise<void>;
};

/** How long the card has been standing, in the operator's words.
 *
 * The prices on a card are a photograph of the moment it was written, and a
 * card can stand for an hour. Without this the desk showed an entry of 68.2895
 * with no hint of whether the scanner had just found it or forgotten it. */
function ageLabel(createdAt: string | undefined, now: number): string | null {
  if (!createdAt) return null;
  const ms = now - new Date(createdAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const min = Math.floor(ms / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  return `${Math.floor(min / 60)}h ago`;
}

/** Desk copy for the live-book reading. The card stays; the button changes.
 *
 * Missing viability is treated as unverified: an older payload must not look
 * more buyable than a current one that failed to read the book.
 */
function viabilityView(v: BuyViability | undefined): {
  buyable: boolean;
  label: string | null;
  className: string;
} {
  const state = v?.state ?? "unverified";
  if (state === "live" && v?.buyable === true) {
    return { buyable: true, label: null, className: "" };
  }
  if (state === "wide") {
    return { buyable: false, label: "Book too wide · waiting", className: "opp-viability--blocked" };
  }
  if (state === "drifted") {
    return {
      buyable: false,
      label: "Price left the card · waiting",
      className: "opp-viability--blocked",
    };
  }
  if (state === "past_setup") {
    return {
      buyable: false,
      label: "Setup already passed",
      className: "opp-viability--blocked",
    };
  }
  return {
    buyable: false,
    label: "Quote unverified · BUY locked",
    className: "opp-viability--blocked",
  };
}

function buyEnabled(
  opp: BuyOpportunity,
  desk: DeskResponse | null,
  busyId: string | null,
): boolean {
  if (busyId !== null) return false;
  if (desk?.session?.entries_allowed === false) return false;
  return viabilityView(opp.viability).buyable;
}

export function OpportunityRail({ desk, scannerLine, onFlash, onRefresh }: Props) {
  const buys = desk?.buy_opportunities ?? [];
  const waits = desk?.entry_watches ?? [];
  const sells = desk?.sell_opportunities ?? [];
  const [busyId, setBusyId] = useState<string | null>(null);

  // Ticks on its own because the desk poll answers 304 when nothing changed,
  // and a card nobody has touched is precisely the one whose age matters. Left
  // to the payload, the label would freeze at the age it was first drawn with.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(t);
  }, []);

  async function onDecide(kind: "buy" | "sell", id: string, act: string, symbol: string) {
    setBusyId(id);
    // Held for the whole request: the answer belongs in the row that announced
    // the question, not on a second card above it.
    let slot: FlashSlot;
    if (kind === "buy" && act === "approve") {
      slot = onFlash(flashPending(`${symbol} · отправляем BUY…`));
    } else if (kind === "sell" && act === "sell") {
      slot = onFlash(flashPending(`${symbol} · отправляем SELL…`));
    } else {
      slot = onFlash(flashPendingLocal(`${symbol} · ${act.toUpperCase()}…`));
    }

    try {
      if (kind === "buy") {
        const data = await decideBuy(id, act === "approve" ? "approve" : "skip");
        if (act === "skip") {
          onFlash(flashSkipOk(symbol), slot);
        } else {
          onFlash(flashBuyOk(symbol, String(data.status || "")), slot);
        }
      } else {
        const data = await decideSell(id, act as "sell" | "hold");
        if (act === "hold") {
          onFlash(flashHoldOk(symbol), slot);
        } else {
          onFlash(flashSellOk(symbol, String(data.status || "")), slot);
        }
      }
      await onRefresh();
    } catch (err) {
      onFlash(humanizeError(err instanceof Error ? err.message : String(err)), slot);
      await onRefresh();
    } finally {
      setBusyId(null);
    }
  }

  const entriesAllowed = desk?.session?.entries_allowed !== false;

  return (
    <aside className="rail">
      <h2>Agent proposals</h2>
      <p className="stage-note">
        You don&apos;t pick tickers. Agents scan the watchlist — you only confirm.
      </p>

      <div className="stage-note" style={{ marginBottom: 12 }}>
        {scannerLine}
      </div>

      {!buys.length && !waits.length && !sells.length ? (
        <div className="block surface">
          <div className="title">No proposals yet</div>
          <div
            className="detail"
            style={{ color: "var(--td-text-muted)", fontFamily: "var(--td-font-sans)" }}
          >
            Scanner is working through the universe. Proposals appear here automatically.
          </div>
        </div>
      ) : null}

      {waits.map((w) => (
        <div className="block block--waiting" key={w.id}>
          <div className="title">{w.symbol}</div>
          <div className="detail">
            THESIS {(w.thesis || "bullish").toUpperCase()} · ENTRY WAIT · Quality{" "}
            {w.entry_quality_at_creation}/100
            <br />
            <span className="opp-levels">
              <span>Now ~{w.current_price_at_creation}</span>
              <span>
                Zone {w.entry_zone_low}–{w.entry_zone_high}
              </span>
              <span>Target {w.planned_target}</span>
            </span>
            <span className="opp-viability opp-viability--blocked">
              No BUY while WAIT — conditions: {(w.required_conditions || []).slice(0, 3).join(", ")}
            </span>
          </div>
        </div>
      ))}

      {buys.map((opp) => {
        const c = opp.candidate;
        const busy = busyId === opp.id;
        const age = ageLabel(opp.created_at, now);
        const viability = viabilityView(opp.viability);
        const canBuy = buyEnabled(opp, desk, busyId);
        return (
          <div className={`block accent${viability.buyable ? "" : " block--waiting"}`} key={opp.id}>
            <div className="title">
              {c.symbol}
            </div>
            <div className="detail">
              {(c.thesis || "bullish").toUpperCase()} · ENTRY GOOD NOW · Quality{" "}
              {c.entry_quality ?? "—"}/100 · Conf {((c.confidence || 0) * 100).toFixed(0)}% · R:R{" "}
              {c.risk_reward} · Qty {formatOppQty(opp)}
              {age ? ` · ${age}` : ""}
              <br />
              <span className="opp-levels">
                <span>Entry {c.entry}</span>
                <span>Stop {c.stop}</span>
                <span>
                  Target {c.target}
                  {c.target_reachability ? ` (${c.target_reachability})` : ""}
                </span>
              </span>
              {!entriesAllowed ? (
                <span className="opp-viability opp-viability--blocked">Outside RTH · entries closed</span>
              ) : viability.label ? (
                <span className={`opp-viability ${viability.className}`}>{viability.label}</span>
              ) : null}
            </div>
            <div className="actions">
              <button
                className="btn-ink"
                type="button"
                disabled={!canBuy}
                title={
                  canBuy
                    ? undefined
                    : viability.label ||
                      (!entriesAllowed ? "Entries closed outside RTH" : "Not buyable")
                }
                onClick={() => onDecide("buy", opp.id, "approve", c.symbol)}
              >
                {busy && busyId === opp.id ? "…" : "BUY"}
              </button>
              <button
                className="btn-light"
                type="button"
                disabled={busyId !== null}
                onClick={() => onDecide("buy", opp.id, "skip", c.symbol)}
              >
                SKIP
              </button>
            </div>
          </div>
        );
      })}

      {sells.map((ex) => {
        const p = ex.proposal;
        const busy = busyId === ex.id;
        return (
          <div className="block ink" key={ex.id}>
            <div className="title">
              {p.symbol}
            </div>
            <div className="detail">
              Entry {p.entry} · Now {p.current} · P&L {p.pnl_pct.toFixed(1)}%
              <br />
              {(p.reasons || []).join(" · ")}
            </div>
            <div className="actions">
              <button
                className="btn-accent"
                type="button"
                disabled={busyId !== null}
                onClick={() => onDecide("sell", ex.id, "sell", p.symbol)}
              >
                {busy ? "…" : "SELL"}
              </button>
              <button
                className="btn-ghost"
                type="button"
                disabled={busyId !== null}
                onClick={() => onDecide("sell", ex.id, "hold", p.symbol)}
              >
                HOLD
              </button>
            </div>
          </div>
        );
      })}
    </aside>
  );
}
