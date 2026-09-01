/** Human-readable desk feedback for success / failure. */

import { t } from "@/i18n";

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
  const text = raw || t("toast.error.unknown");
  const upper = text.toUpperCase();

  if (upper.includes("ALPACA_RATE_LIMIT") || upper.includes("429")) {
    return {
      kind: "error",
      title: t("toast.error.alpacaRateLimit.title"),
      detail: t("toast.error.alpacaRateLimit.detail"),
    };
  }
  if (upper.includes("INTERNAL SERVER ERROR") || text.includes("500")) {
    return {
      kind: "error",
      title: t("toast.error.server500.title"),
      detail: t("toast.error.server500.detail"),
    };
  }
  if (upper.includes("ENTRY_ORDER_REJECTED")) {
    return {
      kind: "error",
      title: t("toast.error.entryRejected.title"),
      detail: t("toast.error.entryRejected.detail"),
    };
  }
  if (upper.includes("FILL_TIMEOUT") || upper.includes("ENTRY_FILL_FAILED")) {
    return {
      kind: "error",
      title: t("toast.error.fillTimeout.title"),
      detail: t("toast.error.fillTimeout.detail"),
    };
  }
  if (upper.includes("STOP_FAILED") || upper.includes("FLATTENED")) {
    return {
      kind: "error",
      title: t("toast.error.stopFailed.title"),
      detail: t("toast.error.stopFailed.detail"),
    };
  }
  if (upper.includes("EXIT_ORDER_REJECTED") || upper.includes("EXIT_FILL_FAILED")) {
    if (upper.includes("HELD_FOR_ORDERS") || upper.includes("INSUFFICIENT QTY") || upper.includes("STILL HOLDING")) {
      return {
        kind: "error",
        title: t("toast.error.exitStopHeld.title"),
        detail: t("toast.error.exitStopHeld.detail"),
      };
    }
    return {
      kind: "error",
      title: t("toast.error.exitFailed.title"),
      detail: t("toast.error.exitFailed.detail"),
    };
  }
  if (upper.includes("KILL_SWITCH")) {
    return {
      kind: "error",
      title: t("toast.error.killSwitch.title"),
      detail: t("toast.error.killSwitch.detail"),
    };
  }
  if (upper.includes("OPERATOR_QTY_ABOVE_RISK")) {
    return {
      kind: "error",
      title: t("toast.error.qtyAboveRisk.title"),
      detail: t("toast.error.qtyAboveRisk.detail"),
    };
  }
  if (upper.includes("OPERATOR_QTY_INVALID")) {
    return {
      kind: "error",
      title: t("toast.error.qtyInvalid.title"),
      detail: t("toast.error.qtyInvalid.detail"),
    };
  }
  if (upper.includes("RISK_REJECT")) {
    return {
      kind: "error",
      title: t("toast.error.riskReject.title"),
      detail: text.replace(/^[^:]*:/, "").trim() || text,
    };
  }
  if (upper.includes("UNAUTHORIZED") || text.includes("401")) {
    return {
      kind: "error",
      title: t("toast.error.unauthorized.title"),
      detail: t("toast.error.unauthorized.detail"),
    };
  }
  if (upper.includes("OPPORTUNITY_EXPIRED") || upper.includes("EXPIRED")) {
    return {
      kind: "error",
      title: t("toast.error.expired.title"),
      detail: t("toast.error.expired.detail"),
    };
  }
  if (upper.includes("LIQUIDITY_GATE_REJECTED") || upper.includes("SPREAD_TOO_WIDE")) {
    if (upper.includes("ENTRY_TOO_FAR_ABOVE_CARD")) {
      return {
        kind: "error",
        title: t("toast.error.entryTooFar.title"),
        detail: t("toast.error.entryTooFar.detail"),
      };
    }
    if (upper.includes("SPREAD_TOO_WIDE")) {
      return {
        kind: "error",
        title: t("toast.error.spreadWide.title"),
        detail: t("toast.error.spreadWide.detail"),
      };
    }
    if (upper.includes("PRICE_MOVED_PAST_SETUP")) {
      return {
        kind: "error",
        title: t("toast.error.pastSetup.title"),
        detail: t("toast.error.pastSetup.detail"),
      };
    }
    return {
      kind: "error",
      title: t("toast.error.liquidityGate.title"),
      detail: text.replace(/^[^:]*:/, "").trim() || text,
    };
  }
  if (upper.includes("INVALID_STATUS")) {
    return {
      kind: "error",
      title: t("toast.error.invalidStatus.title"),
      detail: t("toast.error.invalidStatus.detail"),
    };
  }

  return {
    kind: "error",
    title: t("toast.error.generic.title"),
    detail: text.slice(0, 280),
  };
}

export function flashBuyOk(symbol: string, status: string): FlashMessage {
  if (status === "executed") {
    return {
      kind: "ok",
      title: t("toast.buy.ok.executed.title", { symbol }),
      detail: t("toast.buy.ok.executed.detail"),
    };
  }
  if (status === "discarded") {
    return {
      kind: "error",
      title: t("toast.buy.error.discarded.title", { symbol }),
      detail: t("toast.buy.error.discarded.detail"),
    };
  }
  return {
    kind: "ok",
    title: t("toast.buy.ok.accepted.title", { symbol }),
    detail: t("toast.buy.ok.accepted.detail", { status }),
  };
}

export function flashSkipOk(symbol: string): FlashMessage {
  return {
    kind: "info",
    title: t("toast.skip.ok.title", { symbol }),
    detail: t("toast.skip.ok.detail"),
  };
}

export function flashSellOk(symbol: string, status: string): FlashMessage {
  if (status === "sold") {
    return {
      kind: "ok",
      title: t("toast.sell.ok.sold.title", { symbol }),
      detail: t("toast.sell.ok.sold.detail"),
    };
  }
  return {
    kind: "ok",
    title: t("toast.sell.ok.generic.title", { symbol }),
    detail: t("toast.sell.ok.generic.detail", { status }),
  };
}

export function flashHoldOk(symbol: string): FlashMessage {
  return {
    kind: "info",
    title: t("toast.hold.ok.title", { symbol }),
    detail: t("toast.hold.ok.detail"),
  };
}

export function flashPending(label: string): FlashMessage {
  return {
    kind: "pending",
    title: label,
    detail: t("toast.pending.broker"),
  };
}

/** SKIP and HOLD never reach a broker: they close a card and leave the book
 *  where it was. Promising a fill that nothing is waiting for made the two
 *  cases look alike on screen, and only one of them can move capital. */
export function flashPendingLocal(label: string): FlashMessage {
  return {
    kind: "pending",
    title: label,
    detail: t("toast.pending.local"),
  };
}
