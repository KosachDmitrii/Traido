
import { useState } from "react";
import type { DeskPosition, DeskResponse } from "@/lib/api";
import { closePosition } from "@/lib/api";
import { BROKER_MS, useDesk } from "@/context/DeskContext";
import { flashPending, flashSellOk, humanizeError, type FlashMessage } from "@/lib/messages";
import type { FlashSlot } from "@/lib/toasts";

/** Direction and colour for a position, or nothing at all when there is no mark.
 *
 * A position the broker did not price is rendered without an arrow rather than
 * with a flat one: "we do not know" and "it has not moved" look identical at a
 * glance and mean opposite things about whether the number can be trusted.
 */
function fmtPx(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  // Two decimals is the equity tick the operator compares against the broker;
  // raw ledger strings like 75.138939 look like a different order.
  return n.toFixed(2);
}

function pnlView(p: DeskPosition) {
  if (p.pnl_pct === null || p.pnl_pct === undefined) return null;
  const up = p.pnl_pct >= 0;
  return {
    up,
    arrow: up ? "▲" : "▼",
    className: up ? "pos-pnl pos-pnl--up" : "pos-pnl pos-pnl--down",
    pct: `${up ? "+" : ""}${p.pnl_pct.toFixed(2)}%`,
    cash: p.pnl === null || p.pnl === undefined ? null : Number(p.pnl),
  };
}

export function PositionsReview({ desk }: { desk: DeskResponse | null }) {
  const { showFlash, refreshAll } = useDesk();
  // Two clicks to flatten. The button sits in a list that is otherwise entirely
  // read-only, so a single click here would be the only place on the desk where
  // a stray press spends money.
  const [arming, setArming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function onClose(symbol: string) {
    if (arming !== symbol) {
      setArming(symbol);
      return;
    }
    setArming(null);
    setBusy(symbol);
    let slot: FlashSlot | undefined;
    try {
      slot = showFlash(flashPending(`${symbol} · закрываем позицию…`));
      const result = await closePosition(symbol);
      showFlash(flashSellOk(symbol, String(result?.status ?? "sold")), slot);
      await refreshAll();
    } catch (err) {
      const message: FlashMessage = humanizeError(err instanceof Error ? err.message : String(err));
      showFlash(message, slot);
    } finally {
      setBusy(null);
    }
  }

  // Both ends of the delay, read rather than restated: the server will serve a
  // snapshot for `broker_ttl_seconds`, and the browser asks again every
  // `BROKER_MS`. Written out as a range once and it went stale immediately.
  const ttl = desk?.broker_ttl_seconds;
  const poll = BROKER_MS / 1000;
  const freshness = ttl && ttl < poll ? `${ttl}–${poll}s` : `${poll}s`;

  const positions = desk?.positions ?? [];
  const openOrders = desk?.open_orders ?? [];
  const notes = desk?.review?.notes ?? [];
  const recent = desk?.review?.recent ?? [];

  return (
    <section className="grid-2">
      <div className="card">
        <div className="card-head">
          <div>
            <h2>Positions</h2>
            <div className="sub">Ledger + Alpaca broker ({freshness})</div>
          </div>
        </div>
        <div className="pos-list">
          {positions.length === 0 ? (
            <div className="pos-row pos-row--empty">
              <div className="pos-row__meta">
                <strong>Flat</strong>
                <span>No open positions</span>
              </div>
            </div>
          ) : (
            positions.map((p) => {
              const pnl = pnlView(p);
              const armed = arming === p.symbol;
              return (
                <div className="pos-row" key={p.symbol}>
                  <span className="pos-row__dot" />
                  <div className="pos-row__meta">
                    <strong>{p.symbol}</strong>
                    <span className="pos-row__stats">
                      <span>Qty {p.qty}</span>
                      <span>Entry {fmtPx(p.avg_entry)}</span>
                      {p.mark ? <span>Mark {fmtPx(p.mark)}</span> : null}
                      {p.stop ? <span>Stop {fmtPx(p.stop)}</span> : null}
                      {p.target ? <span>Tgt {fmtPx(p.target)}</span> : null}
                    </span>
                  </div>
                  <div className="pos-row__side">
                    {pnl ? (
                      <div className={pnl.className}>
                        <span className="pos-pnl__arrow" aria-hidden="true">
                          {pnl.arrow}
                        </span>
                        <span className="pos-pnl__figures">
                          <strong>{pnl.pct}</strong>
                          {pnl.cash === null ? null : (
                            <span>
                              {pnl.up ? "+" : ""}
                              {pnl.cash.toFixed(2)}
                            </span>
                          )}
                        </span>
                      </div>
                    ) : (
                      <div className="pos-pnl pos-pnl--unknown" title="No price reported by the broker">
                        <span className="pos-pnl__figures">
                          <strong>—</strong>
                        </span>
                      </div>
                    )}
                    <button
                      type="button"
                      className={armed ? "pos-close pos-close--armed" : "pos-close"}
                      disabled={busy === p.symbol}
                      onClick={() => onClose(p.symbol)}
                      onBlur={() => setArming((s) => (s === p.symbol ? null : s))}
                    >
                      {busy === p.symbol ? "…" : armed ? "Confirm?" : "Close"}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        <div className="sub" style={{ marginTop: 16 }}>
          Open orders · Alpaca
        </div>
        {openOrders.length === 0 ? (
          <p className="empty-hint" style={{ marginTop: 8 }}>
            No resting broker orders
          </p>
        ) : (
          <div className="agent-list">
            {openOrders.map((o) => {
              const px = o.limit_price || o.stop_price || "—";
              return (
                <div className="agent" key={o.broker_order_id || `${o.symbol}-${o.qty}-${px}`}>
                  <span className="dot working" />
                  <div className="meta">
                    <strong>
                      {o.symbol} · {o.side.toUpperCase()} {o.order_type}
                    </strong>
                    <span>
                      Qty {o.qty}
                      {o.filled_qty && o.filled_qty !== "0" ? ` · filled ${o.filled_qty}` : ""}
                      {" · "}
                      {o.limit_price ? `@ ${o.limit_price}` : o.stop_price ? `stop ${o.stop_price}` : "mkt"}
                      {" · "}
                      {o.status}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className="card review-card">
        <div className="card-head">
          <div>
            <h2>Review</h2>
            <div className="sub">Journal analytics · no trading authority</div>
          </div>
        </div>
        <div className="review-body">
          <div className="review-pane">
            <div className="review-feed">
              {notes.length === 0 ? (
                <div className="review-line">
                  <span className="review-line__label">—</span>
                  <span>No review notes yet</span>
                </div>
              ) : (
                notes.map((n, i) => (
                  <div className="review-line" key={i}>
                    <span className="review-line__label">note</span>
                    <span>{n}</span>
                  </div>
                ))
              )}
            </div>
          </div>
          <div className="review-pane">
            <div className="sub">Recent closed</div>
            <div className="review-feed">
              {recent.length === 0 ? (
                <div className="review-line">
                  <span className="review-line__label">—</span>
                  <span>Close a trade to journal</span>
                </div>
              ) : (
                recent.slice(0, 8).map((t, i) => {
                  const pnl = Number(t.pnl);
                  const sign = pnl >= 0 ? "+" : "";
                  return (
                    <div className="review-line" key={i}>
                      <span className="review-line__label">{t.symbol}</span>
                      <span>
                        {sign}
                        {pnl.toFixed(0)} · {(t.pnl_pct || 0).toFixed(1)}%
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
