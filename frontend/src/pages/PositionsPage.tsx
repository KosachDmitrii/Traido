import { useDesk } from "@/context/DeskContext";

export function PositionsPage() {
  const { desk } = useDesk();
  const positions = desk?.positions ?? [];
  const orders = desk?.open_orders ?? [];

  return (
    <section className="card page-card">
      <h3 className="page-section-title">Open positions · {positions.length}</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Qty</th>
              <th>Avg entry</th>
              <th>Stop</th>
              <th>Target</th>
              <th>Strategy</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  Flat — no open positions
                </td>
              </tr>
            ) : (
              positions.map((p) => (
                <tr key={p.symbol}>
                  <td>
                    <strong>{p.symbol}</strong>
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

      <h3 className="page-section-title">Open orders · {orders.length}</h3>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Type</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-hint">
                  No resting broker orders
                </td>
              </tr>
            ) : (
              orders.map((o) => (
                <tr key={o.broker_order_id || `${o.symbol}-${o.qty}`}>
                  <td>
                    <strong>{o.symbol}</strong>
                  </td>
                  <td>{o.side}</td>
                  <td>{o.order_type}</td>
                  <td className="mono">{o.qty}</td>
                  <td className="mono">{o.limit_price || o.stop_price || "mkt"}</td>
                  <td>{o.status}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
