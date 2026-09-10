import { Briefcase, ClipboardList, Wallet, TrendingUp, Layers } from "lucide-react";
import { BROKER_MS, useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import { TablePager, useTablePager } from "@/ui";
import styles from "./PositionsPage.module.css";

function number(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function money(value: unknown, signed = false) {
  const n = number(value);
  return n === null ? "—" : `${signed && n > 0 ? "+" : n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function tone(value: unknown) {
  const n = number(value);
  return n === null || n === 0 ? "neutral" : n > 0 ? "positive" : "negative";
}

export function PositionsPage() {
  const { t, locale } = useI18n();
  const ru = locale === "ru";
  const { desk } = useDesk();
  const positions = desk?.positions ?? [];
  const orders = desk?.open_orders ?? [];
  const posPager = useTablePager(positions);
  const orderPager = useTablePager(orders);
  const verified = desk?.open_orders_verified === true;
  const stats = [
    { icon: Briefcase, label: ru ? "Открытые позиции" : "Open positions", value: desk ? positions.length : "—" },
    { icon: ClipboardList, label: ru ? "Активные ордера" : "Open orders", value: verified ? orders.length : "—" },
    { icon: Wallet, label: ru ? "Капитал счёта" : "Account equity", value: money(desk?.portfolio?.equity) },
    { icon: TrendingUp, label: ru ? "Результат дня" : "Day P&L", value: money(desk?.portfolio?.day_pnl, true), tone: tone(desk?.portfolio?.day_pnl) },
  ];

  return <div className={styles.page}>
    <header className={styles.heading}><div><span className={styles.eyebrow}>ALPACA PAPER</span><h1>{ru ? "Позиции и ордера" : "Positions & orders"}</h1><p>{ru ? "Открытые сделки, их результат и уровни выхода." : "Open trades, their performance and exit levels."}</p></div><span className={styles.refresh}>{ru ? "Обновление" : "Refresh"} · {BROKER_MS / 1000} {ru ? "с" : "s"}</span></header>
    <div className={styles.stats}>{stats.map(({ icon: Icon, label, value, tone: color }) => <div key={label}><span><Icon size={16} aria-hidden />{label}</span><strong data-tone={color}>{value}</strong></div>)}</div>
    <section aria-labelledby="positions-heading">
      <div className={styles.sectionHead}><h2 id="positions-heading">{ru ? "Открытые позиции" : "Open positions"}<span>{desk ? positions.length : "—"}</span></h2><span>{ru ? "Цены и результат · USD" : "Prices & P&L · USD"}</span></div>
      {positions.length === 0 ? <div className={styles.empty} role="status"><Briefcase size={28} aria-hidden /><h3>{!desk || !desk.portfolio ? (ru ? "Получаем данные позиций" : "Waiting for position data") : t("positions.empty")}</h3><p>{ru ? "Здесь появятся открытые сделки и их текущий результат." : "Open trades and their current performance will appear here."}</p></div> : <div className={styles.grid}>
        {posPager.slice.map(p => <article className={styles.position} key={p.symbol}>
          <div className={styles.positionHead}><div className={styles.identity}><span className={styles.symbolIcon}>{p.symbol.slice(0,2)}</span><div><h3>{p.symbol}</h3><p>{p.name || (p.strategy_version || "").split("@")[0] || "Alpaca Paper"}</p></div></div><span className={styles.tag}>{p.qty} {ru ? "акц." : "shares"}</span></div>
          {Number(p.qty) < 0 && <p className={styles.notice} role="alert">{ru ? "Короткая позиция. Требуется сверка исполнений; план покупки к ней не применяется." : "Short position. Execution reconciliation required; the long plan does not apply."}</p>}
          <div className={styles.performance}><div><span>{ru ? "Текущая цена" : "Current price"}</span><strong>{money(p.mark)}</strong></div><div data-tone={tone(p.pnl)}><span>{ru ? "Открытый P&L" : "Unrealized P&L"}</span><strong>{money(p.pnl, true)}</strong><small>{number(p.pnl_pct) === null ? "—" : `${Number(p.pnl_pct) > 0 ? "+" : ""}${Number(p.pnl_pct).toFixed(2)}%`}</small></div></div>
          <dl className={styles.levels}>
            <div><dt>{t("positions.col.avg")}</dt><dd>{money(p.avg_entry)}</dd></div>
            <div><dt>{ru ? "Стоп по плану" : "Planned stop"}</dt><dd>{Number(p.qty) < 0 ? "—" : money(p.stop)}</dd></div>
            <div><dt>{ru ? "План выхода" : "Exit plan"}</dt><dd>{p.exit_policy === "session_close" ? (ru ? "До закрытия сессии" : "Before session close") : money(p.target)}</dd></div>
            <div><dt>{t("positions.col.strategy")}</dt><dd>{p.strategy_version || "—"}</dd></div>
          </dl>
          {p.ledger_linked === false && <p className={styles.notice}>{t("desk.positions.unlinked")}</p>}
        </article>)}
      </div>}
      <TablePager pager={posPager} />
    </section>
    <section className={styles.orders} aria-labelledby="orders-heading">
      <div className={styles.sectionHead}><h2 id="orders-heading">{ru ? "Активные ордера" : "Open orders"}<span>{verified ? orders.length : "—"}</span></h2><span>Alpaca Paper</span></div>
      {!verified && <p className={styles.notice} role="status">{t("desk.orders.unverified")}</p>}
      {orders.length === 0 ? (verified ? <div className={styles.orderEmpty}><Layers size={24} aria-hidden /><h3>{t("orders.empty")}</h3><p>{ru ? "Лимитные и стоп-ордера появятся здесь после размещения." : "Limit and stop orders will appear here after submission."}</p></div> : null) : <div className={styles.tableWrap} tabIndex={0} role="region" aria-label={ru ? "Таблица ордеров" : "Orders table"}>
        <table><thead><tr><th>{t("positions.col.symbol")}</th><th>{t("orders.col.side")}</th><th>{t("orders.col.type")}</th><th>{t("positions.col.qty")}</th><th>{t("orders.col.price")}</th><th>{t("orders.col.status")}</th></tr></thead><tbody>
          {orderPager.slice.map(o => <tr key={o.broker_order_id || `${o.symbol}-${o.qty}`}><td><strong>{o.symbol}</strong></td><td><span className={styles.side} data-side={o.side.toLowerCase()}>{o.side.toUpperCase()}</span></td><td>{o.order_type}</td><td>{o.qty}</td><td>{o.limit_price != null ? money(o.limit_price) : o.stop_price != null ? money(o.stop_price) : t("orders.mkt")}</td><td><span className={styles.orderStatus}>{o.status}</span></td></tr>)}
        </tbody></table>
      </div>}
      <TablePager pager={orderPager} />
    </section>
  </div>;
}
