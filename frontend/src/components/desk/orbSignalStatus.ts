/** Presentation only: never infer a completed setup from the current quote. */
export function orbSignalStatus(state: string | undefined, reasons: string[]): string {
  if (state === "DATA_BLOCKED") return "Нет данных для проверки";
  if (state === "BLOCKED") return "Вход заблокирован";
  const labels: Record<string, string> = {
    ORB_RETEST_WAIT_BREAKOUT: "1/3 · Ждём пробой",
    ORB_RETEST_WAIT_RETURN: "2/3 · Ждём возврат",
    ORB_RETEST_WAIT_CONFIRMATION: "3/3 · Ждём подтверждение",
    ORB_RETEST_CONFIRMED: "Сигнал сформирован · проверяем вход",
    ORB_RETEST_WAIT_RECOVERY: "Ждём восстановления цены",
    ORB_WAITING_PULLBACK: "Ждём цену в зоне покупки",
    ORB_RETEST_EXPIRED: "Сигнал истёк · ждём новый",
    ORB_RETEST_INVALIDATED: "Сигнал отменён · ждём новый",
    ORB_RETEST_REWARD_INSUFFICIENT: "Недостаточно доходности к риску",
    ORB_CLOSED_WAITING_NEW_SIGNAL: "Сделка закрыта · ждём новый сигнал",
  };
  return reasons.map(reason => labels[reason]).find(Boolean) ?? "Проверяем условия сигнала";
}

export function observationTime(value?: string): string | null {
  if (!value || !Number.isFinite(Date.parse(value))) return null;
  return new Date(value).toLocaleTimeString("ru-RU", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
