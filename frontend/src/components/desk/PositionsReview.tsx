
import { useState } from "react";
import { Link } from "react-router-dom";
import type { DeskPosition, DeskResponse } from "@/lib/api";
import { closePosition } from "@/lib/api";
import { BROKER_MS, useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
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
  const t = useT();
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
      slot = showFlash(flashPending(t("toast.pending.close", { symbol })));
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
            <h2>{t("desk.positions.title")}</h2>
            <div className="sub">{t("desk.positions.sub", { freshness })}</div>
          </div>
          <Link className="sub" to="/positions" style={{ color: "inherit" }}>
            {t("desk.positions.link")}
          </Link>
        </div>
        <div className="pos-list">
          {positions.length === 0 ? (
            <div className="pos-row pos-row--empty">
              <div className="pos-row__meta">
                <strong>{t("desk.positions.empty.title")}</strong>
                <span>{t("desk.positions.empty.detail")}</span>
              </div>
            </div>
          ) : (
            positions.map((p) => {
              const pnl = pnlView(p);
              const armed = arming === p.symbol;
              const metrics: { key: string; label: string; value: string }[] = [
                { key: "qty", label: t("desk.positions.stat.qty"), value: String(p.qty) },
                { key: "entry", label: t("desk.positions.stat.entry"), value: fmtPx(p.avg_entry) },
              ];
              if (p.mark) {
                metrics.push({
                  key: "mark",
                  label: t("desk.positions.stat.mark"),
                  value: fmtPx(p.mark),
                });
              }
              if (p.stop) {
                metrics.push({
                  key: "stop",
                  label: t("desk.positions.stat.stop"),
                  value: fmtPx(p.stop),
                });
              }
              if (p.target) {
                metrics.push({
                  key: "tgt",
                  label: t("desk.positions.stat.tgt"),
                  value: fmtPx(p.target),
                });
              }
              return (
                <div className="pos-row" key={p.symbol}>
                  <div className="pos-row__head">
                    <div className="pos-row__title">
                      <span className="pos-row__dot" aria-hidden />
                      <div className="pos-row__identity">
                        <strong>{p.symbol}</strong>
                        {p.name ? <span className="pos-row__name">{p.name}</span> : null}
                      </div>
                    </div>
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
                      <div
                        className="pos-pnl pos-pnl--unknown"
                        title={t("desk.positions.noMark")}
                      >
                        <span className="pos-pnl__figures">
                          <strong>—</strong>
                        </span>
                      </div>
                    )}
                  </div>
                  <div className="pos-row__body">
                    <dl className="pos-row__metrics">
                      {metrics.map((m) => (
                        <div className="pos-metric" key={m.key}>
                          <dt>{m.label}</dt>
                          <dd className="mono">{m.value}</dd>
                        </div>
                      ))}
                    </dl>
                    <button
                      type="button"
                      className={armed ? "pos-close pos-close--armed" : "pos-close"}
                      disabled={busy === p.symbol}
                      onClick={() => onClose(p.symbol)}
                      onBlur={() => setArming((s) => (s === p.symbol ? null : s))}
                    >
                      {busy === p.symbol
                        ? "…"
                        : armed
                          ? t("desk.positions.close.confirm")
                          : t("desk.positions.close")}
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>

        <div className="sub" style={{ marginTop: 16 }}>
          {t("desk.orders.sub")}
        </div>
        {openOrders.length === 0 ? (
          <p className="empty-hint" style={{ marginTop: 8 }}>
            {t("desk.orders.empty")}
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
                      {t("desk.orders.qty")} {o.qty}
                      {o.filled_qty && o.filled_qty !== "0"
                        ? ` · ${t("desk.orders.filled")} ${o.filled_qty}`
                        : ""}
                      {" · "}
                      {o.limit_price
                        ? `@ ${o.limit_price}`
                        : o.stop_price
                          ? `${t("desk.orders.stop")} ${o.stop_price}`
                          : t("desk.orders.mkt")}
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
      <div className="review-stack">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>{t("desk.review.title")}</h2>
              <div className="sub">{t("desk.review.sub")}</div>
            </div>
          </div>
          <div className="review-feed">
            {notes.length === 0 ? (
              <div className="review-line">
                <span className="review-line__label">—</span>
                <span>{t("desk.review.notesEmpty")}</span>
              </div>
            ) : (
              notes.map((n, i) => (
                <div className="review-line" key={i}>
                  <span className="review-line__label">{t("desk.review.noteLabel")}</span>
                  <span>{n}</span>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h2>{t("desk.review.recentTitle")}</h2>
              <div className="sub">{t("desk.review.recentSub")}</div>
            </div>
            <Link className="sub" to="/journal" style={{ color: "inherit" }}>
              {t("desk.review.recentLink")}
            </Link>
          </div>
          <div className="review-feed">
            {recent.length === 0 ? (
              <div className="review-line">
                <span className="review-line__label">—</span>
                <span>{t("desk.review.recentEmpty")}</span>
              </div>
            ) : (
              recent.slice(0, 8).map((trade, i) => {
                const pnl = Number(trade.pnl);
                const sign = pnl >= 0 ? "+" : "";
                return (
                  <div className="review-line" key={i}>
                    <span className="review-line__label">
                      <strong>{trade.symbol}</strong>
                      {trade.name ? <span className="review-line__name">{trade.name}</span> : null}
                    </span>
                    <span>
                      {sign}
                      {pnl.toFixed(0)} · {(trade.pnl_pct || 0).toFixed(1)}%
                    </span>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
