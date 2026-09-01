import { useEffect, useState } from "react";
import { fetchReview, type ReviewPayload } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";

export function JournalPage() {
  const { desk } = useDesk();
  const [review, setReview] = useState<ReviewPayload | null>(desk?.review ?? null);

  useEffect(() => {
    let alive = true;
    fetchReview()
      .then((r) => {
        if (alive) setReview(r);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [desk?.review?.trade_count]);

  const r = review || desk?.review;
  const recent = r?.recent ?? [];

  return (
    <section className="card page-card">
      {(r?.notes || []).length > 0 ? (
        <>
          <h3 className="page-section-title">Notes</h3>
          <ul className="notes-list">
            {(r?.notes || []).map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </>
      ) : null}

      <h3 className="page-section-title">Recent closed</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>PnL</th>
              <th>%</th>
              <th>Strategy</th>
            </tr>
          </thead>
          <tbody>
            {recent.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  Close a trade to journal
                </td>
              </tr>
            ) : (
              recent.map((t, i) => {
                const pnl = Number(t.pnl);
                return (
                  <tr key={t.id || i}>
                    <td>
                      <strong>{t.symbol}</strong>
                    </td>
                    <td className="mono">{t.entry ?? "—"}</td>
                    <td className="mono">{t.exit ?? "—"}</td>
                    <td className={`mono ${pnl >= 0 ? "td-pnl-pos" : "td-pnl-neg"}`}>
                      {pnl >= 0 ? "+" : ""}
                      {pnl.toFixed(2)}
                    </td>
                    <td className="mono">{(t.pnl_pct ?? 0).toFixed(1)}%</td>
                    <td>{(t.strategy_version || "").split("@")[0] || "—"}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
