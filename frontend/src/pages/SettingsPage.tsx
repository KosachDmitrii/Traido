import { useCallback, useState } from "react";
import { runScanner, setKillSwitch } from "@/lib/api";
import { useDesk, type KillSwitchState } from "@/context/DeskContext";

const KILL_LABEL: Record<KillSwitchState, string> = {
  loading: "Reading…",
  on: "ON — disable",
  off: "OFF — enable",
  unreadable: "Unreadable",
};

export function SettingsPage() {
  // Read from the shared state the header also reads, so the page and the
  // header can never disagree about whether trading is blocked.
  const { desk, refreshAll, showFlash, killSwitch: kill, refreshKillSwitch } = useDesk();
  const [apiKey, setApiKey] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("TRAIDO_API_KEY") || "" : "",
  );
  const [busy, setBusy] = useState(false);

  const saveKey = useCallback(() => {
    if (apiKey.trim()) window.localStorage.setItem("TRAIDO_API_KEY", apiKey.trim());
    else window.localStorage.removeItem("TRAIDO_API_KEY");
    showFlash({ kind: "ok", title: "API key saved", detail: "Used as X-API-Key for /api requests." });
  }, [apiKey, showFlash]);

  const toggleKill = useCallback(async () => {
    setBusy(true);
    try {
      const next = await setKillSwitch(kill !== "on");
      showFlash({
        kind: next.enabled ? "error" : "ok",
        title: next.enabled ? "Kill switch ON" : "Kill switch OFF",
        detail: next.enabled ? "New orders blocked." : "Confirmations allowed again.",
      });
    } catch (err) {
      showFlash({
        kind: "error",
        title: "Kill switch failed",
        detail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      // Re-read rather than trust the response, so a failed toggle leaves the
      // switch showing what the server actually has.
      await refreshKillSwitch();
      setBusy(false);
    }
  }, [kill, showFlash, refreshKillSwitch]);

  const scanNow = useCallback(async () => {
    setBusy(true);
    try {
      await runScanner();
      await refreshAll();
      showFlash({ kind: "info", title: "Scan started", detail: "Universe pass requested." });
    } finally {
      setBusy(false);
    }
  }, [refreshAll, showFlash]);

  const universe = desk?.scanner?.universe ?? [];

  return (
    <section className="card page-card">
      <h3 className="page-section-title">Safety</h3>
      <div className="settings-row">
        <div>
          <strong>Kill switch</strong>
          <div className="sub">Blocks all new broker orders when on</div>
        </div>
        <button
          type="button"
          className={kill === "on" ? "btn-ink" : "btn-ghost"}
          disabled={busy || kill === "loading" || kill === "unreadable"}
          onClick={toggleKill}
        >
          {KILL_LABEL[kill]}
        </button>
      </div>

      <h3 className="page-section-title">API key</h3>
      <div className="settings-row">
        <input
          className="logs-search"
          placeholder="TRAIDO_API_KEY (optional)"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <button type="button" className="btn-ghost" onClick={saveKey}>
          Save
        </button>
      </div>

      <h3 className="page-section-title">Scanner</h3>
      <div className="settings-row">
        <div>
          <strong>Universe</strong>
          <div className="sub">
            {universe.length
              ? `${universe.length} symbols · ${universe[0]}…${universe[universe.length - 1]} · curated names always kept; rest ranked for scan quality`
              : "—"}
          </div>
        </div>
        <button type="button" className="btn-ink" disabled={busy} onClick={scanNow}>
          Run scan now
        </button>
      </div>

      <h3 className="page-section-title">About</h3>
      <p className="sub">
        Traido confirmation desk · Vite + React frontend · FastAPI backend · paper Alpaca only in V1.
      </p>
    </section>
  );
}
