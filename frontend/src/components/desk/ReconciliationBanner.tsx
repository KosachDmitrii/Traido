import type { DeskResponse } from "@/lib/api";

/**
 * Says out loud when the desk has stopped checking itself against the broker.
 *
 * Reconciliation is what turns the local book into broker truth: it finds
 * unprotected positions, resolves unknown orders, and absorbs fills nobody
 * told us about. When it fails, every number on this desk is still rendered —
 * it is just the book's own opinion. That distinction has to be visible, not
 * buried in a server log, because it is the difference between "we hold this"
 * and "we last believed we held this".
 */
export function ReconciliationBanner({ desk }: { desk: DeskResponse | null }) {
  const state = desk?.reconciliation;
  if (!state || state.ok !== false) return null;

  const stale = state.stale_seconds;
  const since =
    stale == null
      ? "с момента запуска ни одна сверка не прошла"
      : `последняя успешная — ${formatAge(stale)} назад`;

  return (
    <div className="flash flash--error" role="alert" aria-live="assertive">
      <div className="flash__body">
        <strong className="flash__title">Сверка с брокером не проходит</strong>
        <p className="flash__detail">
          Цифры ниже — состояние локальной книги, а не подтверждённая позиция у брокера ({since}).
          {state.error ? ` ${state.error}` : ""}
        </p>
      </div>
    </div>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${seconds} с`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} мин`;
  return `${Math.round(minutes / 60)} ч`;
}
