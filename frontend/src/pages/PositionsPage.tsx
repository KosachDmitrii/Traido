import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import { TablePager, useTablePager } from "@/ui";

export function PositionsPage() {
  const t = useT();
  const { desk } = useDesk();
  const positions = desk?.positions ?? [];
  const orders = desk?.open_orders ?? [];
  const posPager = useTablePager(positions);
  const orderPager = useTablePager(orders);

  return (
    <section className="card page-card">
      <h3 className="page-section-title">{t("positions.page.open", { n: positions.length })}</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>{t("positions.col.symbol")}</th>
              <th>{t("positions.col.qty")}</th>
              <th>{t("positions.col.avg")}</th>
              <th>{t("positions.col.stop")}</th>
              <th>{t("positions.col.target")}</th>
              <th>{t("positions.col.strategy")}</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  {t("positions.empty")}
                </td>
              </tr>
            ) : (
              posPager.slice.map((p) => (
                <tr key={p.symbol}>
                  <td>
                    <div className="symbol-cell">
                      <strong>{p.symbol}</strong>
                      {p.name ? <span className="symbol-cell__name">{p.name}</span> : null}
                    </div>
                  </td>
                  <td className="mono">{p.qty}</td>
                  <td className="mono">{p.avg_entry}</td>
                  <td className="mono">{p.stop ?? "—"}</td>
                  <td className="mono">{p.target ?? "—"}</td>
                  <td>{(p.strategy_version || "").split("@")[0] || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <TablePager pager={posPager} />

      <h3 className="page-section-title">{t("positions.page.orders", { n: orders.length })}</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>{t("positions.col.symbol")}</th>
              <th>{t("orders.col.side")}</th>
              <th>{t("orders.col.type")}</th>
              <th>{t("positions.col.qty")}</th>
              <th>{t("orders.col.price")}</th>
              <th>{t("orders.col.status")}</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  {t("orders.empty")}
                </td>
              </tr>
            ) : (
              orderPager.slice.map((o) => (
                <tr key={o.broker_order_id || `${o.symbol}-${o.qty}`}>
                  <td>
                    <strong>{o.symbol}</strong>
                  </td>
                  <td>{o.side}</td>
                  <td>{o.order_type}</td>
                  <td className="mono">{o.qty}</td>
                  <td className="mono">{o.limit_price || o.stop_price || t("orders.mkt")}</td>
                  <td>{o.status}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <TablePager pager={orderPager} />
    </section>
  );
}
