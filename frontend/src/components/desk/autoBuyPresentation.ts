import type { OrbExecution } from "@/lib/api";
import { orbReason } from "./orbLabels";

export function autoBuyPresentation(execution?: OrbExecution, planState?: string, available = true) {
  const stage = execution?.stage ?? (planState === "SKIPPED" ? "SKIPPED" : "OBSERVING");
  const states: Record<string, [string, string, string]> = {
    OBSERVING: ["Наблюдение", "Ждём условий для автоматической покупки.", "neutral"],
    WAITING: ["Ожидает проверки", "Предложение готово. Автопокупка ожидает обработки.", "neutral"],
    QUEUED: ["В очереди", "Предложение поставлено в очередь автоматической покупки.", "neutral"],
    CHECKING: ["Проверяем покупку", "Проверяем свежую цену, риск и состояние счёта.", "active"],
    CHECKING_EXECUTION: ["Покупка обрабатывается", "Проверяем состояние исполнения. Покупка ещё не подтверждена.", "active"],
    SUBMITTED: ["Заявка отправлена", "Ожидаем подтверждения исполнения у брокера.", "active"],
    APPROVED: ["Заявка одобрена", "Ожидаем подтверждения исполнения у брокера.", "active"],
    EXECUTED: ["Куплено", "Исполнение подтверждено. Позиция доступна на странице «Позиции».", "success"],
    WAIT: ["Ожидаем условия", "Последняя попытка не прошла. Повторим проверку автоматически.", "neutral"],
    DATA_BLOCKED: ["Ждём данные", "Покупка приостановлена до получения достоверных данных.", "warning"],
    OPERATIONAL_BLOCKED: ["Покупка приостановлена", "Проверка счёта или сервиса не пройдена. Предусмотрен повтор.", "warning"],
    UNKNOWN: ["Уточняем исполнение", "Результат неизвестен. Повторная покупка заблокирована до сверки.", "warning"],
    NO_TRADE: ["Отказано", "Покупка не прошла обязательные проверки.", "warning"],
    TERMINAL_REJECT: ["Отказано", "Покупка отклонена. Повтор по этому предложению не выполняется.", "warning"],
    DISCARDED: ["Отказано", "Предложение снято. Покупка по нему больше не выполняется.", "warning"],
    EXPIRED: ["Время истекло", "Срок покупки по этому предложению закончился.", "neutral"],
    SKIPPED: ["Пропущено сейчас", "Ждём нового движения цены перед повторным предложением.", "neutral"],
  };
  const [title, detail, tone] = !available ? ["Автопокупка недоступна", "Автоматическая покупка недоступна в текущем режиме.", "warning"]
    : states[stage] ?? ["Уточняем статус", "Получаем состояние предложения с сервера.", "neutral"];
  const terminal = ["EXECUTED", "DISCARDED", "EXPIRED", "SKIPPED", "TERMINAL_REJECT", "NO_TRADE"].includes(stage);
  const loading = available && ["CHECKING", "CHECKING_EXECUTION", "SUBMITTED", "APPROVED"].includes(stage);
  const codes = execution?.last_error?.split(/[:,|]/).map(s => s.trim()).filter(Boolean) ?? [];
  const translated = codes.map(orbReason).filter((text, i) => text !== codes[i]);
  const lastError = !terminal && execution?.last_error ? translated.join(" · ") || "Обязательная проверка перед покупкой не пройдена." : null;
  return { title, detail, tone, loading, terminal, lastError };
}
