const reasons: Record<string,string> = {
  ORB_ATTEMPT_CONSUMED:"Решение по этому плану уже принято. Повторного входа сегодня не будет.",
  ORB_PUBLICATION_UNRESOLVED:"Связь с предложением требует восстановления. Повторный вход заблокирован.",
  ORB_SIP_SUBSCRIPTION_REQUIRED:"Подписка Alpaca не разрешает свежие данные SIP. Подключите доступ к SIP в Plans & Features → Market Data. Доступ проверяется автоматически каждую минуту.",
  ORB_SIP_ACCESS_DENIED:"Alpaca отказала в доступе к SIP (403). Проверьте подписку на данные SIP и используемые API-ключи. Новые входы недоступны.",
  ORB_DATA_CREDENTIALS_REJECTED:"Alpaca отклонила API-ключи (401). Требуется исправить подключение данных.",
  ORB_DATA_RATE_LIMITED:"Alpaca ограничила частоту запросов (429). Повторяем после паузы.",
  ORB_LOADING:"Загружаем данные открытия из Alpaca SIP.",
  ORB_NO_CANDIDATES:"В этой сессии нет кандидатов, прошедших условия ORB. Причины — в статистике отбора.",
  ORB_OPENING_RANGE_FORMING:"Ждём завершения диапазона 09:30–09:35 по Нью-Йорку.",
  ORB_OUTSIDE_ENTRY_SESSION:"Входы доступны после 09:35 ET до установленного времени окончания входов.",
  ORB_SIP_REQUIRED:"Для объёма ORB нужен доступ Alpaca SIP. Данные IEX не подменяют этот источник.",
  ORB_WAITING_BREAKOUT:"Ждём bid выше максимума открытия. Геометрия плана зафиксирована.",
  ORB_BREAKOUT_CONFIRMED:"Пробой подтверждён котировкой; перед отправкой повторно проверяются цена и риск счёта.",
  ORB_ENTRY_MISSED:"Цена выше предела покупки. Вход сейчас запрещён.",
  ORB_ENTRY_EXPIRED:"Время новых входов по этому плану истекло.",
  ORB_QUOTE_STALE:"Ждём свежую котировку Alpaca SIP.",
  ORB_QUOTE_MISSING:"Нет достоверной котировки.",
  ORB_SERVICE_UNAVAILABLE:"Не удалось проверить данные или счёт. Повторная проверка выполняется автоматически.",
  ORB_HISTORY_INCOMPLETE:"Нет полной истории для 14 предыдущих сессий.",
  ORB_DAILY_VOLUME_LOW:"Средний дневной объём меньше 1 млн акций.",
  ORB_ATR_LOW:"Дневной ATR не превышает $0.50.",
  ORB_PRICE_BELOW_MINIMUM:"Цена открытия не превышает $5.",
  ORB_RELATIVE_VOLUME_LOW:"Объём первых пяти минут меньше среднего объёма того же окна за 14 сессий.",
  ORB_OPENING_NOT_BULLISH:"Первая пятиминутная свеча не растущая; эта версия торгует только покупки.",
  WEEKLY_PNL_UNAVAILABLE:"Недоступен результат периода риска счёта.",
  PORTFOLIO_DRAWDOWN_UNAVAILABLE:"Недоступна просадка счёта.",
};
export const orbReason = (reason: string) => reasons[reason] ?? reason;
export const orbState = (state?:string) => ({EXECUTED:"Исполнено",SKIPPED:"Пропущено",EXPIRED:"Истёк",DISCARDED:"Снят",APPROVING:"Проверяем исполнение",APPROVED:"Одобрено",WAIT:"Ожидание пробоя",BUY_ALLOWED:"Проверка входа",BLOCKED:"Вход заблокирован",DATA_BLOCKED:"Нет данных",NO_TRADE:"Нет входа"}[state ?? ""] ?? "План");
export const px = (value: unknown) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(2);
export const etTime = (value:string) => new Date(value).toLocaleTimeString("ru-RU",{timeZone:"America/New_York",hour:"2-digit",minute:"2-digit"});
