import { runScanner, invalidateDeskEtag, setAutoTrigger, setBrokerBackend, setEntryPolicy, setKillSwitch, fetchAutoTrigger, fetchBrokerBackend, fetchEntryPolicy } from "@/lib/api";
import { executionBrokerLabelKey } from "@/lib/brokerLabel";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n";
import type { Vars } from "@/i18n/store";
import { Button, Input, SegmentedControl, SwitchControl } from "@/ui";
import { useCallback, useEffect, useRef, useState } from "react";
import { Gauge, KeyRound, Languages, ScanSearch, ShieldAlert, Building2, Zap } from "lucide-react";

/** Five production desk steps — Сильно → Слабо. Values match backend ENTRY_LEVELS. */
const ENTRY_STEPS = [
  { value: 0, key: "settings.entry.strong" as const },
  { value: 25, key: "settings.entry.firmer" as const },
  { value: 50, key: "settings.entry.medium" as const },
  { value: 75, key: "settings.entry.softer" as const },
  { value: 100, key: "settings.entry.weak" as const },
] as const;

const BROKER_STEPS = [
  { value: "alpaca", key: "settings.broker.alpaca" as const },
  { value: "ibkr", key: "settings.broker.ibkr" as const },
] as const;

function snapEntryStep(n: number): number {
  let best: number = ENTRY_STEPS[0].value;
  for (const step of ENTRY_STEPS) {
    if (Math.abs(step.value - n) < Math.abs(best - n)) best = step.value;
  }
  return best;
}

function entryLabelKey(aggressiveness: number): MessageKey {
  const step = ENTRY_STEPS.find((s) => s.value === snapEntryStep(aggressiveness));
  return step?.key ?? "settings.entry.strong";
}

function entryStepDetailKey(aggressiveness: number): MessageKey {
  const step = snapEntryStep(aggressiveness);
  return (`settings.entry.step.${step}` as MessageKey);
}


type Translate = (key: MessageKey, vars?: Vars) => string;

function brokerConnectionStateLabel(t: Translate, state: string | undefined): string {
  const raw = (state ?? "").trim().toUpperCase();
  if (!raw) return "—";
  if (raw === "READY") return t("settings.broker.state.ready");
  if (raw === "DISCONNECTED") return t("settings.broker.state.disconnected");
  if (raw === "DEGRADED") return t("settings.broker.state.degraded");
  if (raw === "CONNECTING") return t("settings.broker.state.connecting");
  if (raw === "RECONNECTING") return t("settings.broker.state.reconnecting");
  return t("settings.broker.state.unknown", { state: state ?? raw });
}

function brokerBlockedReasonLabel(t: Translate, reason: string): string {
  const openPos = /^open_positions:(.+)$/i.exec(reason);
  if (openPos) {
    return t("settings.broker.blocked.openPositions", { symbols: openPos[1] });
  }
  const unknown = /^unknown_intents:(\d+)$/i.exec(reason);
  if (unknown) {
    return t("settings.broker.blocked.unknownIntents", { n: Number(unknown[1]) });
  }
  const openIntents = /^open_intents:(\d+)$/i.exec(reason);
  if (openIntents) {
    return t("settings.broker.blocked.openIntents", { n: Number(openIntents[1]) });
  }
  return reason;
}

export function SettingsPage() {
  const { desk, refreshAll, showFlash, killSwitch: kill, refreshKillSwitch } = useDesk();
  const { t, locale, setLocale } = useI18n();
  const [apiKey, setApiKey] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("TRAIDO_API_KEY") || "" : "",
  );
  const [busy, setBusy] = useState(false);
  const [settingsReady, setSettingsReady] = useState(false);
  const [aggressiveness, setAggressiveness] = useState<number | null>(null);
  const [brokerBackend, setBrokerBackendState] = useState<"alpaca" | "ibkr" | null>(null);
  const [autoTrigger, setAutoTriggerState] = useState<boolean | null>(null);
  const [autoTriggerAvailable, setAutoTriggerAvailable] = useState(true);
  const [autoTriggerNote, setAutoTriggerNote] = useState<string | null>(null);
  const entrySaveGen = useRef(0);
  const brokerSaveGen = useRef(0);
  const triggerSaveGen = useRef(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [entry, trigger, broker] = await Promise.all([
          fetchEntryPolicy(),
          fetchAutoTrigger(),
          fetchBrokerBackend(),
        ]);
        if (cancelled) return;
        if (entrySaveGen.current === 0) {
          setAggressiveness(snapEntryStep(entry.aggressiveness));
        }
        if (triggerSaveGen.current === 0) {
          setAutoTriggerState(trigger.enabled);
          setAutoTriggerAvailable(trigger.available !== false);
          setAutoTriggerNote(trigger.note ?? null);
        }
        if (brokerSaveGen.current === 0) {
          setBrokerBackendState(broker.backend === "ibkr" ? "ibkr" : "alpaca");
        }
        setSettingsReady(true);
      } catch {
        if (!cancelled) setSettingsReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const n = desk?.entry_policy?.aggressiveness;
    // Do not let a stale desk poll overwrite a save still in flight.
    if (entrySaveGen.current !== 0) return;
    if (typeof n === "number") setAggressiveness(snapEntryStep(n));
  }, [desk?.entry_policy?.aggressiveness]);

  useEffect(() => {
    const b = desk?.broker_backend?.backend;
    if (brokerSaveGen.current !== 0) return;
    if (b === "alpaca" || b === "ibkr") setBrokerBackendState(b);
  }, [desk?.broker_backend?.backend]);

  useEffect(() => {
    const trigger = desk?.auto_trigger;
    if (triggerSaveGen.current !== 0) return;
    if (typeof trigger?.enabled === "boolean") setAutoTriggerState(trigger.enabled);
    if (trigger?.available !== undefined) setAutoTriggerAvailable(trigger.available !== false);
    if (trigger?.note) setAutoTriggerNote(trigger.note);
  }, [desk?.auto_trigger]);

  const saveKey = useCallback(() => {
    if (apiKey.trim()) {
      window.localStorage.setItem("TRAIDO_API_KEY", apiKey.trim());
      showFlash({
        kind: "ok",
        title: t("settings.api.saved.title"),
        detail: t("settings.api.saved.detail"),
      });
    } else {
      window.localStorage.removeItem("TRAIDO_API_KEY");
      showFlash({
        kind: "info",
        title: t("settings.api.cleared.title"),
        detail: t("settings.api.cleared.detail"),
      });
    }
  }, [apiKey, showFlash, t]);

  const clearKey = useCallback(() => {
    setApiKey("");
    window.localStorage.removeItem("TRAIDO_API_KEY");
    showFlash({
      kind: "info",
      title: t("settings.api.cleared.title"),
      detail: t("settings.api.cleared.detail"),
    });
  }, [showFlash, t]);

  const toggleKill = useCallback(async () => {
    setBusy(true);
    try {
      const next = await setKillSwitch(kill !== "on");
      showFlash({
        kind: next.enabled ? "error" : "ok",
        title: next.enabled ? t("settings.kill.flash.on.title") : t("settings.kill.flash.off.title"),
        detail: next.enabled
          ? t("settings.kill.flash.on.detail")
          : t("settings.kill.flash.off.detail"),
      });
    } catch (err) {
      showFlash({
        kind: "error",
        title: t("settings.kill.flash.failed"),
        detail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      await refreshKillSwitch();
      setBusy(false);
    }
  }, [kill, showFlash, refreshKillSwitch, t]);

  const commitEntryPolicy = useCallback(
    async (value: number) => {
      const stepped = snapEntryStep(value);
      const gen = ++entrySaveGen.current;
      try {
        const next = await setEntryPolicy(stepped);
        setAggressiveness(snapEntryStep(next.aggressiveness));
        invalidateDeskEtag();
        const aborted = Boolean(next.rescan?.aborted);
        showFlash({
          kind: "ok",
          title: t("settings.entry.flash.title"),
          detail: aborted
            ? t("settings.entry.flash.detailAbort", { n: t(entryLabelKey(next.aggressiveness)) })
            : t("settings.entry.flash.detail", { n: t(entryLabelKey(next.aggressiveness)) }),
        });
        await refreshAll();
      } catch (err) {
        showFlash({
          kind: "error",
          title: t("settings.entry.flash.failed"),
          detail: err instanceof Error ? err.message : String(err),
        });
      } finally {
        if (entrySaveGen.current === gen) entrySaveGen.current = 0;
      }
    },
    [refreshAll, showFlash, t],
  );

  const commitBrokerBackend = useCallback(
    async (value: string) => {
      const backend = value === "ibkr" ? "ibkr" : "alpaca";
      const gen = ++brokerSaveGen.current;
      try {
        const next = await setBrokerBackend(backend);
        setBrokerBackendState(next.backend === "ibkr" ? "ibkr" : "alpaca");
        invalidateDeskEtag();
        showFlash({
          kind: "ok",
          title: t("settings.broker.flash.title"),
          detail: t("settings.broker.flash.detail", { n: t(executionBrokerLabelKey(next.backend)) }),
        });
        await refreshAll();
      } catch (err) {
        showFlash({
          kind: "error",
          title: t("settings.broker.flash.failed"),
          detail: err instanceof Error ? err.message : String(err),
        });
        const current = desk?.broker_backend?.backend;
        if (current === "alpaca" || current === "ibkr") setBrokerBackendState(current);
      } finally {
        if (brokerSaveGen.current === gen) brokerSaveGen.current = 0;
      }
    },
    [desk?.broker_backend?.backend, refreshAll, showFlash, t],
  );

  const toggleAutoTrigger = useCallback(async () => {
    if (autoTrigger === null || !autoTriggerAvailable) return;
    const next = !autoTrigger;
    setAutoTriggerState(next);
    const gen = ++triggerSaveGen.current;
    setBusy(true);
    try {
      const result = await setAutoTrigger(next);
      setAutoTriggerState(result.enabled);
      setAutoTriggerAvailable(result.available !== false);
      setAutoTriggerNote(result.note ?? null);
      invalidateDeskEtag();
      if (next && !result.enabled) {
        showFlash({
          kind: "error",
          title: t("settings.trigger.flash.failed"),
          detail: result.note ?? t("settings.trigger.flash.rejected.detail"),
        });
      } else {
        showFlash({
          kind: result.enabled ? "info" : "ok",
          title: result.enabled
            ? t("settings.trigger.flash.on.title")
            : t("settings.trigger.flash.off.title"),
          detail: result.enabled
            ? t("settings.trigger.flash.on.detail")
            : t("settings.trigger.flash.off.detail"),
        });
      }
      await refreshAll();
    } catch (err) {
      setAutoTriggerState(!next);
      showFlash({
        kind: "error",
        title: t("settings.trigger.flash.failed"),
        detail: err instanceof Error ? err.message : String(err),
      });
    } finally {
      if (triggerSaveGen.current === gen) triggerSaveGen.current = 0;
      setBusy(false);
    }
  }, [autoTrigger, autoTriggerAvailable, refreshAll, showFlash, t]);

  const scanNow = useCallback(async () => {
    setBusy(true);
    try {
      await runScanner();
      await refreshAll();
      showFlash({
        kind: "info",
        title: t("settings.scanner.flash.title"),
        detail: t("settings.scanner.flash.detail"),
      });
    } finally {
      setBusy(false);
    }
  }, [refreshAll, showFlash, t]);

  const universe = desk?.scanner?.universe ?? [];
  const killBadgeLabel =
    kill === "on"
      ? t("settings.kill.badge.on")
      : kill === "off"
        ? t("settings.kill.badge.off")
        : kill === "loading"
          ? t("settings.kill.badge.loading")
          : t("settings.kill.badge.unreadable");

  const controlsDisabled = busy || !settingsReady;

  return (
    <section className="settings-page">
      <header className="settings-hero">
        <h2 className="settings-hero__title">{t("settings.title")}</h2>
        <p className="settings-hero__intro">{t("settings.intro")}</p>
      </header>

      <article className={`settings-card${kill === "on" ? " settings-card--danger" : ""}`}>
        <div className="settings-card__icon" aria-hidden>
          <ShieldAlert size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.kill.title")}</h3>
            <span
              className={`settings-badge${kill === "on" ? " settings-badge--on" : ""}${kill === "unreadable" ? " settings-badge--warn" : ""}`}
            >
              {killBadgeLabel}
            </span>
          </div>
          <p className="settings-card__lead">{t("settings.kill.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.kill.what")}</li>
            <li>{t("settings.kill.keeps")}</li>
            <li>{t("settings.kill.when")}</li>
          </ul>
          <div className="settings-kill-control">
            <div className="settings-kill-control__copy">
              <strong>
                {kill === "on" ? t("settings.kill.disable") : t("settings.kill.enable")}
              </strong>
              <span>{t("settings.kill.lead")}</span>
            </div>
            <SwitchControl
              checked={kill === "on"}
              onCheckedChange={() => void toggleKill()}
              disabled={busy || kill === "loading" || kill === "unreadable"}
              aria-label={
                kill === "on" ? t("settings.kill.disable") : t("settings.kill.enable")
              }
            />
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Zap size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.trigger.title")}</h3>
            <span className={`settings-badge${autoTrigger ? " settings-badge--on" : ""}`}>
              {!settingsReady || autoTrigger === null
                ? "…"
                : autoTrigger
                  ? t("settings.trigger.badge.on")
                  : t("settings.trigger.badge.off")}
            </span>
          </div>
          <p className="settings-card__lead">{t("settings.trigger.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.trigger.what")}</li>
            <li>{t("settings.trigger.keeps")}</li>
            <li>{t("settings.trigger.when")}</li>
          </ul>
          {!autoTriggerAvailable && autoTriggerNote ? (
            <p className="settings-card__lead">{autoTriggerNote}</p>
          ) : null}
          <div className="settings-kill-control">
            <div className="settings-kill-control__copy">
              <strong>
                {autoTrigger ? t("settings.trigger.disable") : t("settings.trigger.enable")}
              </strong>
              <span>{t("settings.trigger.lead")}</span>
            </div>
            <SwitchControl
              checked={autoTrigger ?? false}
              onCheckedChange={() => void toggleAutoTrigger()}
              disabled={controlsDisabled || !autoTriggerAvailable}
              aria-label={
                autoTrigger ? t("settings.trigger.disable") : t("settings.trigger.enable")
              }
            />
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Gauge size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.entry.title")}</h3>
            <span className="settings-badge">
              {settingsReady && aggressiveness !== null
                ? t(entryLabelKey(aggressiveness))
                : "…"}
            </span>
          </div>
          <p className="settings-card__lead">{t("settings.entry.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.entry.what")}</li>
            <li>{t("settings.entry.keeps")}</li>
            <li>{t("settings.entry.hint")}</li>
          </ul>
          <div className="settings-entry-steps">
            <div className="settings-entry-steps__ends" aria-hidden>
              <span>{t("settings.entry.strongHint")}</span>
              <span>{t("settings.entry.weakHint")}</span>
            </div>
            <SegmentedControl
              wide
              ariaLabel={t("settings.entry.title")}
              value={String(aggressiveness ?? 0)}
              onChange={(v) => {
                if (controlsDisabled) return;
                const n = snapEntryStep(Number(v));
                setAggressiveness(n);
                void commitEntryPolicy(n);
              }}
              options={ENTRY_STEPS.map((step) => ({
                value: String(step.value),
                label: t(step.key),
              }))}
            />
            <p className="settings-entry-steps__detail">
              {settingsReady && aggressiveness !== null
                ? t(entryStepDetailKey(aggressiveness))
                : ""}
            </p>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Building2 size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.broker.title")}</h3>
            <span className="settings-badge">
              {settingsReady && brokerBackend
                ? t(executionBrokerLabelKey(brokerBackend))
                : "…"}
            </span>
          </div>
          <p className="settings-card__lead">{t("settings.broker.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.broker.what")}</li>
            <li>{t("settings.broker.keeps")}</li>
            <li>{t("settings.broker.hint")}</li>
          </ul>
          <p className="settings-card__lead">
            {t("settings.broker.status", {
              state: brokerConnectionStateLabel(t, desk?.broker_backend?.connection_state),
            })}
            {desk?.broker_backend?.account_id
              ? ` · ${t("settings.broker.account", { id: desk.broker_backend.account_id })}`
              : null}
          </p>
          {desk?.broker_backend?.switch_blocked_reason ? (
            <p className="settings-card__lead">
              {t("settings.broker.blocked", {
                reason: brokerBlockedReasonLabel(t, desk.broker_backend.switch_blocked_reason),
              })}
            </p>
          ) : null}
          <div className="settings-entry-steps">
            <SegmentedControl
              wide
              ariaLabel={t("settings.broker.title")}
              value={brokerBackend ?? "alpaca"}
              onChange={(v) => {
                if (controlsDisabled) return;
                setBrokerBackendState(v as "alpaca" | "ibkr");
                void commitBrokerBackend(v);
              }}
              options={BROKER_STEPS.map((step) => ({
                value: step.value,
                label: t(step.key),
              }))}
            />
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <KeyRound size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.api.title")}</h3>
          </div>
          <p className="settings-card__lead">{t("settings.api.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.api.what")}</li>
            <li>{t("settings.api.hint")}</li>
          </ul>
          <div className="settings-card__actions settings-card__actions--stack">
            <Input
              className="logs-search"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={t("settings.api.placeholder")}
              value={apiKey}
              onValueChange={setApiKey}
            />
            <div className="settings-card__btnrow">
              <Button variant="ink" onClick={saveKey}>
                {t("settings.api.save")}
              </Button>
              <Button variant="ghost" onClick={clearKey} disabled={!apiKey}>
                {t("settings.api.clear")}
              </Button>
            </div>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <ScanSearch size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.scanner.title")}</h3>
            {universe.length ? (
              <span className="settings-badge">{universe.length}</span>
            ) : null}
          </div>
          <p className="settings-card__lead">{t("settings.scanner.lead")}</p>
          <ul className="settings-points">
            <li>
              {universe.length
                ? t("settings.scanner.what", {
                    n: universe.length,
                    first: universe[0],
                    last: universe[universe.length - 1],
                  })
                : t("settings.scanner.empty")}
            </li>
            <li>{t("settings.scanner.hint")}</li>
          </ul>
          <div className="settings-card__actions">
            <Button variant="accent" disabled={busy} onClick={scanNow}>
              {t("settings.scanner.run")}
            </Button>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Languages size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.lang.title")}</h3>
          </div>
          <p className="settings-card__lead">{t("settings.lang.lead")}</p>
          <ul className="settings-points">
            <li>{t("settings.lang.what")}</li>
          </ul>
          <div className="settings-card__actions">
            <SegmentedControl
              ariaLabel={t("lang.switch")}
              value={locale}
              onChange={(code) => setLocale(code as Locale)}
              options={[
                { value: "en", label: t("lang.en") },
                { value: "ru", label: t("lang.ru") },
              ]}
            />
          </div>
        </div>
      </article>

      <article className="settings-card settings-card--muted">
        <div className="settings-card__body">
          <h3 className="settings-about-title">{t("settings.about.title")}</h3>
          <p className="settings-about-body">{t("settings.about.body")}</p>
        </div>
      </article>
    </section>
  );
}
