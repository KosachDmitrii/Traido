import type { MessageKey } from "@/i18n/en";

/** Label key for the execution broker selected in settings (Alpaca vs IBKR). */
export function executionBrokerLabelKey(
  backend: string | null | undefined,
): MessageKey {
  return backend === "ibkr" ? "settings.broker.ibkr" : "settings.broker.alpaca";
}
