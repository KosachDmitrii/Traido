/** Human-readable desk feedback for success / failure. */

export type FlashKind = "ok" | "error" | "pending" | "info";

export type FlashMessage = {
  kind: FlashKind;
  title: string;
  detail?: string;
  /** Stays until it is dismissed by hand. For the few messages that report
   *  something unresolved, where letting a clock clear it would be the same as
   *  claiming it resolved. */
  sticky?: boolean;
};

function detailToString(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === "object" && d && "msg" in d ? String((d as { msg: unknown }).msg) : String(d)))
      .join("; ");
  }
  if (typeof detail === "object" && detail && "msg" in detail) {
    return String((detail as { msg: unknown }).msg);
  }
  return String(detail);
}

export function parseApiError(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const s = detailToString((payload as { detail: unknown }).detail);
    if (s) return s;
  }
  return fallback;
}

/** Map backend codes to plain language. */
export function humanizeError(raw: string): FlashMessage {
  const text = raw || "Unknown error";
  const upper = text.toUpperCase();

  if (upper.includes("ALPACA_RATE_LIMIT") || upper.includes("429")) {
    return {
      kind: "error",
      title: "Alpaca rate limit",
      detail: "Слишком много запросов к paper API. Подожди пару секунд — desk уже реже поллит.",
    };
  }
  if (upper.includes("INTERNAL SERVER ERROR") || text.includes("500")) {
    return {
      kind: "error",
      title: "Связь с API оборвалась",
      detail:
        "Часто вне сессии US лимит ждёт fill дольше, чем держит прокси. Карточка должна вернуться — нажми BUY ещё раз или обнови desk.",
    };
  }
  if (upper.includes("ENTRY_ORDER_REJECTED")) {
    return {
      kind: "error",
      title: "BUY отклонён брокером",
      detail: "Ордер не принят (часто тик цены). Карточка снова в очереди — можно повторить.",
    };
  }
  if (upper.includes("FILL_TIMEOUT") || upper.includes("ENTRY_FILL_FAILED")) {
    return {
      kind: "error",
      title: "BUY не исполнен",
      detail:
        "Лимит не заполнился (вне RTH US почти всегда). Ордер отменён, карточка снова в очереди.",
    };
  }
  if (upper.includes("STOP_FAILED") || upper.includes("FLATTENED")) {
    return {
      kind: "error",
      title: "Stop не встал — позиция закрыта",
      detail: "Защитный stop не принят брокером. Сработал emergency flatten. Позиция не оставлена голой.",
    };
  }
  if (upper.includes("EXIT_ORDER_REJECTED") || upper.includes("EXIT_FILL_FAILED")) {
    return {
      kind: "error",
      title: "SELL не исполнен",
      detail: "Выход не прошёл. Предложение возвращено в очередь — попробуй ещё раз.",
    };
  }
  if (upper.includes("KILL_SWITCH")) {
    return {
      kind: "error",
      title: "Kill switch включён",
      detail: "Торговля заблокирована. Выключи kill switch, чтобы снова подтверждать сделки.",
    };
  }
  if (upper.includes("RISK_REJECT")) {
    return {
      kind: "error",
      title: "Risk Engine отклонил",
      detail: text.replace(/^[^:]*:/, "").trim() || text,
    };
  }
  if (upper.includes("UNAUTHORIZED") || text.includes("401")) {
    return {
      kind: "error",
      title: "Нет доступа к API",
      detail: "Проверь TRAIDO_API_KEY в localStorage или открой desk с localhost.",
    };
  }
  if (upper.includes("OPPORTUNITY_EXPIRED") || upper.includes("EXPIRED")) {
    return {
      kind: "error",
      title: "Предложение истекло",
      detail: "TTL карточки вышел. Дождись нового скана.",
    };
  }
  if (upper.includes("LIQUIDITY_GATE_REJECTED") || upper.includes("SPREAD_TOO_WIDE")) {
    if (upper.includes("ENTRY_TOO_FAR_ABOVE_CARD")) {
      return {
        kind: "error",
        title: "Цена ушла от карточки",
        detail:
          "Рынок ушёл выше Entry больше чем на 0.25R. Карточка остаётся — BUY снова станет доступен, когда книга вернётся.",
      };
    }
    if (upper.includes("SPREAD_TOO_WIDE")) {
      return {
        kind: "error",
        title: "Слишком широкий спред",
        detail: "В такую книгу не входим. Карточка остаётся — дождись сужения спреда.",
      };
    }
    if (upper.includes("PRICE_MOVED_PAST_SETUP")) {
      return {
        kind: "error",
        title: "Сетап уже пройден",
        detail: "Цена прошла Stop или Target. Это уже не та сделка — SKIP или новый скан.",
      };
    }
    return {
      kind: "error",
      title: "Liquidity gate отказал",
      detail: text.replace(/^[^:]*:/, "").trim() || text,
    };
  }
  if (upper.includes("INVALID_STATUS")) {
    return {
      kind: "error",
      title: "Уже обработано",
      detail: "Это предложение уже в другом статусе (повторный клик или параллельный запрос).",
    };
  }

  return {
    kind: "error",
    title: "Запрос не прошёл",
    detail: text.slice(0, 280),
  };
}

export function flashBuyOk(symbol: string, status: string): FlashMessage {
  if (status === "executed") {
    return {
      kind: "ok",
      title: `${symbol} · BUY исполнен`,
      detail: "Fill получен, защитный stop выставлен, позиция в ledger. Смотри блок Positions.",
    };
  }
  if (status === "discarded") {
    return {
      kind: "error",
      title: `${symbol} · BUY отклонён системой`,
      detail: "Сделка не осталась открытой (fill/stop problem). Проверь Activity / audit.",
    };
  }
  return {
    kind: "ok",
    title: `${symbol} · BUY принято`,
    detail: `Статус: ${status}`,
  };
}

export function flashSkipOk(symbol: string): FlashMessage {
  return {
    kind: "info",
    title: `${symbol} · SKIP`,
    detail: "Предложение отклонено, ордер не отправлялся.",
  };
}

export function flashSellOk(symbol: string, status: string): FlashMessage {
  if (status === "sold") {
    return {
      kind: "ok",
      title: `${symbol} · SELL исполнен`,
      detail: "Позиция закрыта по fill, сделка записана в journal. Win rate обновится после refresh.",
    };
  }
  return {
    kind: "ok",
    title: `${symbol} · SELL`,
    detail: `Статус: ${status}`,
  };
}

export function flashHoldOk(symbol: string): FlashMessage {
  return {
    kind: "info",
    title: `${symbol} · HOLD`,
    detail: "Выход отложен, позиция остаётся открытой.",
  };
}

export function flashPending(label: string): FlashMessage {
  return {
    kind: "pending",
    title: label,
    detail: "Ждём ответ брокера (fill может занять до ~45 с)…",
  };
}

/** SKIP and HOLD never reach a broker: they close a card and leave the book
 *  where it was. Promising a fill that nothing is waiting for made the two
 *  cases look alike on screen, and only one of them can move capital. */
export function flashPendingLocal(label: string): FlashMessage {
  return {
    kind: "pending",
    title: label,
    detail: "Записываем решение — ордер не отправляется.",
  };
}
