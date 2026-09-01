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

function buyable(opp: BuyOpportunity, entriesAllowed: boolean, busy: boolean): boolean {
  if (busy || !entriesAllowed) return false;
  return opp.viability?.buyable === true && opp.viability.state === "live";
}

function viabilityLabel(opp: BuyOpportunity, entriesAllowed: boolean): string | null {
  if (!entriesAllowed) return "Outside RTH · entries closed";
  const state = opp.viability?.state ?? "unverified";
  if (state === "live" && opp.viability?.buyable) return null;
  if (state === "wide") return "Book too wide · waiting";
  if (state === "drifted") return "Price left the card · waiting";
  if (state === "past_setup") return "Setup already passed";
  return "Quote unverified · BUY locked";
}

export function OpportunitiesPage() {
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
    const label = `${symbol} · ${act.toUpperCase()}…`;
    const slot = showFlash(reachesBroker ? flashPending(label) : flashPendingLocal(label));
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
      <h3 className="page-section-title">Buy proposals · {buys.length}</h3>
      <div className="opp-grid">
        {buys.length === 0 ? (
          <div className="empty-hint">No open BUY cards. Scanner will refill the queue.</div>
        ) : (
          buys.map((opp) => {
            const c = opp.candidate;
            const canBuy = buyable(opp, entriesAllowed, busyId !== null);
            const note = viabilityLabel(opp, entriesAllowed);
            return (
              <div className="opp-card" key={opp.id}>
                <div className="opp-card__head">
                  <strong>{c.symbol}</strong>
                  <span>Conf {(c.confidence * 100).toFixed(0)}% · R:R {c.risk_reward}</span>
                </div>
                <div className="opp-card__meta mono">
                  Entry {c.entry} · Stop {c.stop} · Tgt {c.target}
                  {opp.risk?.sized_qty ? ` · Qty ${opp.risk.sized_qty}` : ""}
                </div>
                {note ? <div className="opp-viability opp-viability--blocked">{note}</div> : null}
                <div className="opp-card__actions">
                  <button
                    type="button"
                    className="btn-ink"
                    disabled={!canBuy}
                    onClick={() => onDecide("buy", opp.id, "approve", c.symbol)}
                  >
                    BUY
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busyId !== null}
                    onClick={() => onDecide("buy", opp.id, "skip", c.symbol)}
                  >
                    SKIP
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>

      <h3 className="page-section-title">Sell proposals · {sells.length}</h3>
      <div className="opp-grid">
        {sells.length === 0 ? (
          <div className="empty-hint">No exit proposals. Open a position first.</div>
        ) : (
          sells.map((ex) => {
            const p = ex.proposal;
            const busy = busyId === ex.id;
            return (
              <div className="opp-card" key={ex.id}>
                <div className="opp-card__head">
                  <strong>{p.symbol}</strong>
                  <span>PnL {p.pnl_pct.toFixed(1)}%</span>
                </div>
                <div className="opp-card__meta mono">
                  Entry {p.entry} · Now {p.current}
                </div>
                <div className="opp-card__actions">
                  <button
                    type="button"
                    className="btn-ink"
                    disabled={busy}
                    onClick={() => onDecide("sell", ex.id, "sell", p.symbol)}
                  >
                    SELL
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => onDecide("sell", ex.id, "hold", p.symbol)}
                  >
                    HOLD
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
