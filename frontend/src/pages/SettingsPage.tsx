import { runScanner, invalidateDeskEtag, setEntryPolicy, setKillSwitch } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n";
import { Button, Input, SegmentedControl, SwitchControl } from "@/ui";
import { useCallback, useEffect, useRef, useState } from "react";
import { Gauge, KeyRound, Languages, ScanSearch, ShieldAlert } from "lucide-react";

/** Five production desk steps — Сильно → Слабо. Values match backend ENTRY_LEVELS. */
const ENTRY_STEPS = [
  { value: 0, key: "settings.entry.strong" as const },
  { value: 25, key: "settings.entry.firmer" as const },
  { value: 50, key: "settings.entry.medium" as const },
  { value: 75, key: "settings.entry.softer" as const },
  { value: 100, key: "settings.entry.weak" as const },
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

export function SettingsPage() {
  const { desk, refreshAll, showFlash, killSwitch: kill, refreshKillSwitch } = useDesk();
  const { t, locale, setLocale } = useI18n();
  const [apiKey, setApiKey] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("TRAIDO_API_KEY") || "" : "",
  );
  const [busy, setBusy] = useState(false);
  const [aggressiveness, setAggressiveness] = useState(
    () => snapEntryStep(desk?.entry_policy?.aggressiveness ?? 0),
  );
  const entrySaveGen = useRef(0);

  useEffect(() => {
    const n = desk?.entry_policy?.aggressiveness;
    // Do not let a stale desk poll overwrite a save still in flight.
    if (entrySaveGen.current !== 0) return;
    if (typeof n === "number") setAggressiveness(snapEntryStep(n));
  }, [desk?.entry_policy?.aggressiveness]);

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
          <Gauge size={20} strokeWidth={1.5} absoluteStrokeWidth />
        </div>
        <div className="settings-card__body">
          <div className="settings-card__head">
            <h3>{t("settings.entry.title")}</h3>
            <span className="settings-badge">{t(entryLabelKey(aggressiveness))}</span>
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
              value={String(aggressiveness)}
              onChange={(v) => {
                const n = snapEntryStep(Number(v));
                setAggressiveness(n);
                void commitEntryPolicy(n);
              }}
              options={ENTRY_STEPS.map((step) => ({
                value: String(step.value),
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
