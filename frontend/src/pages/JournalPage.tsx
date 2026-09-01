import { useEffect, useState } from "react";
import { fetchReview, type ReviewPayload } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import { TablePager, useTablePager } from "@/ui";

export function JournalPage() {
  const t = useT();
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
  const pager = useTablePager(recent);

  return (
    <section className="card page-card">
      {(r?.notes || []).length > 0 ? (
        <>
          <h3 className="page-section-title">{t("journal.notes")}</h3>
          <ul className="notes-list">
            {(r?.notes || []).map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </>
      ) : null}

      <h3 className="page-section-title">{t("journal.recent")}</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>{t("journal.col.symbol")}</th>
              <th>{t("journal.col.entry")}</th>
              <th>{t("journal.col.exit")}</th>
              <th>{t("journal.col.pnl")}</th>
              <th>{t("journal.col.pct")}</th>
              <th>{t("journal.col.strategy")}</th>
            </tr>
          </thead>
          <tbody>
            {recent.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  {t("journal.empty")}
                </td>
              </tr>
            ) : (
              pager.slice.map((trade, i) => {
                const pnl = Number(trade.pnl);
                return (
                  <tr key={trade.id || i}>
                    <td>
                      <div className="symbol-cell">
                        <strong>{trade.symbol}</strong>
                        {trade.name ? <span className="symbol-cell__name">{trade.name}</span> : null}
                      </div>
                    </td>
                    <td className="mono">{trade.entry ?? "—"}</td>
                    <td className="mono">{trade.exit ?? "—"}</td>
                    <td className={`mono ${pnl >= 0 ? "td-pnl-pos" : "td-pnl-neg"}`}>
                      {pnl >= 0 ? "+" : ""}
                      {pnl.toFixed(2)}
                    </td>
                    <td className="mono">{(trade.pnl_pct ?? 0).toFixed(2)}%</td>
                    <td>{(trade.strategy_version || "").split("@")[0] || "—"}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <TablePager pager={pager} />
    </section>
  );
}
