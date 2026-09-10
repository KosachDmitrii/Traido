import panels from "@/styles/SettingsPanels.module.css";
import { useEffect, useState } from "react";
import { useI18n } from "@/i18n/I18nProvider";
import { invalidateDeskEtag, paperRiskPeriod, type PaperRiskSnapshot } from "@/lib/api";
import { Button, LoadingDots } from "@/ui";

export function PaperRiskPeriod() {
  const { t, locale } = useI18n();
  const ru = locale === "ru";
  const [snapshot, setSnapshot] = useState<PaperRiskSnapshot | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (busy) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await paperRiskPeriod();
        if (!stopped) { setSnapshot(next); setError(""); }
      } catch (e) {
        if (!stopped) { setSnapshot(null); setError(String(e)); }
      } finally {
        if (!stopped) timer = setTimeout(() => void refresh(), 15000);
      }
    };
    void refresh();
    return () => { stopped = true; clearTimeout(timer); };
  }, [busy]);

  async function commit(action: "start" | "suspend") {
    if (!snapshot?.risk_account_id || busy || !confirmed) return;
    setBusy(true);
    try {
      setSnapshot(await paperRiskPeriod(action, snapshot.risk_account_id));
      setConfirmed(false); setError(""); invalidateDeskEtag();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const active = snapshot?.risk_history_status === "observed_period";
  const startable = snapshot?.risk_history_status === "not_started";
  const money = (value: string | number | null | undefined) => value == null ? "—" : new Intl.NumberFormat(ru ? "ru-RU" : "en-US", { style: "currency", currency: "USD" }).format(Number(value));
  const status = active ? (ru ? "Наблюдение активно" : "Observation active") : startable ? (ru ? "Не начат" : "Not started") : snapshot?.risk_history_status === "suspended" ? (ru ? "Приостановлен" : "Suspended") : (ru ? "Статус недоступен" : "Status unavailable");
  return <section className={panels.risk} aria-label={t("riskPeriod.title")}>
    <div className={panels.heading}><h4>{ru ? "Учёт риска" : "Risk tracking"}</h4><span className={`${panels.badge} ${active ? panels.ready : ""}`}>{status}</span></div>
    <p className={panels.subtitle}>{ru ? "Капитал и просадка с начала наблюдения." : "Equity and drawdown since observation began."}</p>
    {error && <p role="alert" className={panels.error}>{error}</p>}
    {!snapshot && !error && <LoadingDots ariaLabel={t("common.loading")} />}
    {snapshot && <>
      <dl className={panels.metrics}>
        <div><dt>{ru ? "Текущий капитал" : "Current equity"}</dt><dd>{money(snapshot.equity)}</dd></div>
        <div><dt>{ru ? "Изменение от наблюдаемой базы недели" : "Change from observed weekly baseline"}</dt><dd>{active ? money(snapshot.week_pnl) : "—"}</dd></div>
        <div><dt>{ru ? "Наблюдаемая просадка" : "Observed drawdown"}</dt><dd>{active && snapshot.drawdown_pct != null ? `${snapshot.drawdown_pct.toFixed(2)}%` : "—"}</dd></div>
      </dl>
      {snapshot.risk_period_started_at && <p className={panels.subtitle}>{t("riskPeriod.started", { date: new Date(snapshot.risk_period_started_at).toLocaleString(ru ? "ru-RU" : "en-US") })}</p>}
      <details className={panels.details}><summary>{ru ? "Как рассчитываются показатели" : "How metrics are calculated"}</summary><p>{t("riskPeriod.description")}</p><p className={panels.account}>{ru ? "Счёт: " : "Account: "}{snapshot.risk_account_id ?? "—"}</p></details>
      <div className={panels.notice}>{t("riskPeriod.fundingWarning")}</div>
      {(active || startable) && <div className={panels.actions}>
        <label className={panels.confirm}><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} /><span>{t(active ? "riskPeriod.confirmSuspend" : "riskPeriod.confirmStart")}</span></label>
        <Button variant={active ? "light" : "accent"} loading={busy} disabled={busy || !confirmed} onClick={() => void commit(active ? "suspend" : "start")}>
          {t(active ? "riskPeriod.suspend" : "riskPeriod.start")}
        </Button>
      </div>}
    </>}
  </section>;
}
