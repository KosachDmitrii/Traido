
import type { DeskResponse } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";

function money(v: string | number | undefined | null, digits = 0): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function StatsRow({ desk }: { desk: DeskResponse | null }) {
  const t = useT();
  const pf = desk?.portfolio;
  const rev = desk?.review;
  const openCount = desk?.positions?.length ?? pf?.open_positions ?? 0;
  const openOrders = desk?.open_orders?.length ?? pf?.open_orders ?? 0;
  const day = Number(pf?.day_pnl);
  const wr =
    rev && rev.trade_count ? `${(rev.win_rate * 100).toFixed(0)}%` : "—";

  return (
    <section className="stats">
      <div className="stat">
        <div className="label">{t("stats.equity")}</div>
        <div className="row">
          <div className="value mono">{money(pf?.equity)}</div>
        </div>
      </div>
      <div className="stat">
        <div className="label">{t("stats.cash")}</div>
        <div className="row">
          <div className="value mono">{money(pf?.cash)}</div>
        </div>
      </div>
      <div className="stat">
        <div className="label">{t("stats.buyingPower")}</div>
        <div className="row">
          <div className="value mono">{money(pf?.buying_power)}</div>
        </div>
      </div>
      <div className="stat">
        <div className="label">{t("stats.todayPnl")}</div>
        <div className="row">
          <div
            className={`value mono ${
              Number.isFinite(day) ? (day >= 0 ? "td-pnl-pos" : "td-pnl-neg") : ""
            }`}
          >
            {Number.isFinite(day)
              ? `${day >= 0 ? "+" : "-"}$${Math.abs(day).toFixed(0)}`
              : "—"}
          </div>
        </div>
      </div>
      <div className="stat">
        <div className="label">{t("stats.positions")}</div>
        <div className="row">
          <div className="value">{openCount}</div>
          <span className="td-pill td-pill--muted">{t("stats.ordersPill", { n: openOrders })}</span>
        </div>
      </div>
      <div className="stat">
        <div className="label">{t("stats.winRate")}</div>
        <div className="row">
          <div className="value mono">{wr}</div>
          <span className="td-pill td-pill--muted">
            {t("stats.tradesPill", { n: rev?.trade_count ?? 0 })}
          </span>
        </div>
      </div>
    </section>
  );
}
