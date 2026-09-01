import { useCallback, useState } from "react";
import { KeyRound, Languages, Radar, ShieldAlert } from "lucide-react";
import { runScanner, setKillSwitch } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n";

export function SettingsPage() {
  const { desk, refreshAll, showFlash, killSwitch: kill, refreshKillSwitch } = useDesk();
  const { t, locale, setLocale } = useI18n();
  const [apiKey, setApiKey] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("TRAIDO_API_KEY") || "" : "",
  );
  const [busy, setBusy] = useState(false);

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
          <ShieldAlert size={22} strokeWidth={1.75} absoluteStrokeWidth />
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
          <div className="settings-card__actions">
            <button
              type="button"
              className={kill === "on" ? "btn-ghost" : "btn-ink"}
              disabled={busy || kill === "loading" || kill === "unreadable"}
              onClick={toggleKill}
            >
              {kill === "on" ? t("settings.kill.disable") : t("settings.kill.enable")}
            </button>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <KeyRound size={22} strokeWidth={1.75} absoluteStrokeWidth />
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
            <input
              className="logs-search"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={t("settings.api.placeholder")}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
            <div className="settings-card__btnrow">
              <button type="button" className="btn-ink" onClick={saveKey}>
                {t("settings.api.save")}
              </button>
              <button type="button" className="btn-ghost" onClick={clearKey} disabled={!apiKey}>
                {t("settings.api.clear")}
              </button>
            </div>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Radar size={22} strokeWidth={1.75} absoluteStrokeWidth />
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
            <button type="button" className="btn-ink" disabled={busy} onClick={scanNow}>
              {t("settings.scanner.run")}
            </button>
          </div>
        </div>
      </article>

      <article className="settings-card">
        <div className="settings-card__icon" aria-hidden>
          <Languages size={22} strokeWidth={1.75} absoluteStrokeWidth />
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
            <div className="lang-toggle" role="group" aria-label={t("lang.switch")}>
              {(["en", "ru"] as Locale[]).map((code) => (
                <button
                  key={code}
                  type="button"
                  className={`lang-toggle__btn${locale === code ? " is-active" : ""}`}
                  onClick={() => setLocale(code)}
                  aria-pressed={locale === code}
                >
                  {t(code === "en" ? "lang.en" : "lang.ru")}
                </button>
              ))}
            </div>
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
