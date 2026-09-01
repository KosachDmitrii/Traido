import type { DeskResponse } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";

export function ReconciliationBanner({ desk }: { desk: DeskResponse | null }) {
  const t = useT();
  const state = desk?.reconciliation;
  if (!state || state.ok !== false) return null;

  const stale = state.stale_seconds;
  const since =
    stale == null
      ? t("recon.never")
      : t("recon.stale", { age: formatAge(stale, t) });

  return (
    <div className="flash flash--error" role="alert" aria-live="assertive">
      <div className="flash__body">
        <strong className="flash__title">{t("recon.title")}</strong>
        <p className="flash__detail">
          {t("recon.detail", { since })}
          {state.error ? ` ${state.error}` : ""}
        </p>
      </div>
    </div>
  );
}

function formatAge(seconds: number, t: ReturnType<typeof useT>): string {
  if (seconds < 90) return t("recon.age.seconds", { n: seconds });
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return t("recon.age.minutes", { n: minutes });
  return t("recon.age.hours", { n: Math.round(minutes / 60) });
}
