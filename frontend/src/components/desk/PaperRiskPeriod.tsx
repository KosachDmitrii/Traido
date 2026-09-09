import { useEffect, useState } from "react";
import { useT } from "@/i18n/I18nProvider";
import { invalidateDeskEtag, paperRiskPeriod, type PaperRiskSnapshot } from "@/lib/api";
import { Button } from "@/ui";

export function PaperRiskPeriod() {
  const t = useT();
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
        if (!stopped) { setSnapshot(next); }
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
  return <section aria-label={t("riskPeriod.title")}>
    <h4>{t("riskPeriod.title")}</h4>
    <p className="settings-card__lead">{t("riskPeriod.description")}</p>
    {error && <p role="alert">{error}</p>}
    {snapshot && <>
      <p>{t("riskPeriod.account", { account: snapshot.risk_account_id ?? "—", equity: snapshot.equity })}</p>
      <p>{t("riskPeriod.status", { status: snapshot.risk_history_status ?? "—" })}</p>
      {snapshot.risk_period_started_at && <p>{t("riskPeriod.started", { date: new Date(snapshot.risk_period_started_at).toLocaleString() })}</p>}
      {active && <p>{t("riskPeriod.metrics", { pnl: snapshot.week_pnl ?? "—", dd: snapshot.drawdown_pct?.toFixed(2) ?? "—" })}</p>}
      <p className="settings-card__lead">{t("riskPeriod.fundingWarning")}</p>
      {(active || startable) && <>
        <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} /> {t(active ? "riskPeriod.confirmSuspend" : "riskPeriod.confirmStart")}</label>
        <div className="settings-card__actions"><Button disabled={busy || !confirmed} onClick={() => void commit(active ? "suspend" : "start")}>
          {t(active ? "riskPeriod.suspend" : "riskPeriod.start")}
        </Button></div>
      </>}
    </>}
  </section>;
}
