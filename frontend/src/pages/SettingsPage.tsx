import panels from "@/styles/SettingsPanels.module.css";
import { runScanner, setKillSwitch, setAutoTrigger } from "@/lib/api";
import { executionBrokerLabelKey } from "@/lib/brokerLabel";
import { PaperRiskPeriod } from "@/components/desk/PaperRiskPeriod";
import { useDesk } from "@/context/DeskContext";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale, MessageKey } from "@/i18n";
import type { Vars } from "@/i18n/store";
import { Button, Input, SegmentedControl, SwitchControl, LoadingDots } from "@/ui";
import { useCallback, useState } from "react";
import { KeyRound, Languages, ScanSearch, ShieldAlert, Building2 } from "lucide-react";

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

export function SettingsPage() {
  const { desk, refreshAll, showFlash, killSwitch: kill, refreshKillSwitch } = useDesk();
  const { t, locale, setLocale } = useI18n();
  const [apiKey, setApiKey] = useState(() =>
    typeof window !== "undefined" ? window.localStorage.getItem("TRAIDO_API_KEY") || "" : "",
  );
  const [busy, setBusy] = useState(false);
  const [triggerBusy, setTriggerBusy] = useState(false);
  const [triggerError, setTriggerError] = useState("");
  const trigger = desk?.auto_trigger;
  const toggleTrigger = async (enabled: boolean) => {
    setTriggerBusy(true);
    setTriggerError("");
    try {
      await setAutoTrigger(enabled);
      await refreshAll();
    } catch (error) {
      setTriggerError(error instanceof Error ? error.message : String(error));
    } finally { setTriggerBusy(false); }
  };
  const [scanning, setScanning] = useState(false);
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
    setScanning(true);
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
      setScanning(false);
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
        <div className="settings-card__body">
          <h3>{locale === "ru" ? "Автоматическая покупка" : "Automatic buying"}</h3>
          <p className="settings-card__lead">{locale === "ru"
            ? "При включении система сама покупает по допущенным планам ORB, включая уже ожидающие предложения. Перед каждой покупкой заново проверяются цена, срок входа и риск."
            : "When enabled, eligible ORB plans are bought automatically, including pending proposals. Price, entry deadline and risk are rechecked before every purchase."}</p>
          <div className="settings-kill-control">
            <div className="settings-kill-control__copy">
              <strong>{!trigger ? (locale === "ru" ? "Статус недоступен" : "Status unavailable") : trigger.enabled ? (locale === "ru" ? "Включена" : "On") : (locale === "ru" ? "Выключена" : "Off")}</strong>
              <span>{locale === "ru" ? "Только Paper. Kill switch и все проверки риска действуют." : "Paper only. Kill switch and all risk checks remain active."}</span>
            </div>
            {triggerBusy && <LoadingDots ariaLabel={t("common.loading")} />}
            <SwitchControl checked={trigger?.enabled ?? false} onCheckedChange={(enabled) => void toggleTrigger(enabled)}
              disabled={triggerBusy || !trigger || (!trigger.available && !trigger.enabled)}
              aria-label={locale === "ru" ? "Автоматическая покупка" : "Automatic buying"} />
          </div>
          {triggerError && <p role="alert">{triggerError}</p>}
        </div>
      </article>

      <article className={`settings-card ${panels.strategy}`}>
        <div className="settings-card__body">
          <div className="settings-card__head"><h3>Opening Range Breakout</h3><span className={panels.badge}>ORB · Paper</span></div>
          <p className={panels.subtitle}>{locale === "ru" ? "Возврат к уровню после роста, подтверждение и ограниченная зона покупки." : "Retest after a breakout, confirmation and a capped entry zone."}</p>
          <dl className={panels.metrics}>
            <div><dt>{locale === "ru" ? "Диапазон открытия" : "Opening range"}</dt><dd>09:30–09:35 <small>ET</small></dd></div>
            <div><dt>{locale === "ru" ? "Отбор" : "Selection"}</dt><dd>{locale === "ru" ? "Относительный объём" : "Relative volume"}</dd></div>
            <div><dt>{locale === "ru" ? "Вход" : "Entry"}</dt><dd>{!trigger ? "—" : trigger.enabled ? (locale === "ru" ? "Автоматический" : "Automatic") : (locale === "ru" ? "С подтверждением" : "Confirmation")}</dd></div>
            <div><dt>{locale === "ru" ? "Выход" : "Exit"}</dt><dd>{locale === "ru" ? "Цель / стоп / время" : "Target / stop / time"}</dd></div>
          </dl>
          <p className={panels.note}>{locale === "ru" ? "Новый сигнал действует 10 минут. Цена и риск проверяются перед покупкой. Правила и параметры — в паспорте стратегии." : "A new signal lasts 10 minutes. Price and risk are checked before buying. See the strategy passport for rules and parameters."}</p>
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
              {t(executionBrokerLabelKey(desk?.broker_backend?.backend))}
            </span>
          </div>
          <dl className={panels.metrics}>
            <div><dt>{locale === "ru" ? "Подключение" : "Connection"}</dt><dd className={desk?.broker_backend?.connection_state?.toUpperCase() === "READY" ? panels.ready : undefined}>{brokerConnectionStateLabel(t, desk?.broker_backend?.connection_state)}</dd></div>
            <div><dt>{locale === "ru" ? "Рыночные данные" : "Market data"}</dt><dd>Alpaca {desk?.orb?.feed?.toUpperCase() ?? "—"}</dd></div>
            <div><dt>{locale === "ru" ? "Среда исполнения" : "Execution environment"}</dt><dd>{desk?.broker_backend?.environment ?? "—"}</dd></div>
          </dl>
          <details className={panels.details}>
            <summary>{locale === "ru" ? "Счёт и особенности исполнения" : "Account and execution details"}</summary>
            <p className={panels.account}>{desk?.broker_backend?.account_id ?? "—"}</p>
            <p>{t("settings.broker.keeps")}</p>
            <p>{t("settings.broker.hint")}</p>
          </details>
          {desk?.broker_backend?.broker_class === "MockPaperBroker" ? (
            <p className="settings-card__lead">{t("settings.broker.mockWarning")}</p>
          ) : null}
          <PaperRiskPeriod />
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
            <Button variant="accent" loading={scanning} disabled={busy} onClick={scanNow}>
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
