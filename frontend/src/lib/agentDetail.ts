import type { AgentState } from "./api";
import type { useT } from "@/i18n/I18nProvider";

/** Human-readable last result shared by the board and desk. */
export function agentDetail(agent: AgentState, t: ReturnType<typeof useT>): string {
  let detail = agent.detail || agent.last_symbol || t("agents.waiting");
  if (/SECTOR_BLOCKED/.test(detail) && !/MISSING|STALE|UNAVAILABLE|INVALID/i.test(detail)) {
    const regime = detail.match(/SECTOR_REGIME:(bearish|risk_off|high_volatility)/i)?.[1]?.toLowerCase();
    if (regime === "bearish") detail = t("agents.reason.sectorBearish");
    if (regime === "risk_off") detail = t("agents.reason.sectorRiskOff");
    if (regime === "high_volatility") detail = t("agents.reason.sectorVolatile");
  }
  if (detail === "Entry rejected before portfolio risk") detail = t("agents.reason.beforeRisk");
  return agent.last_symbol && (["error", "rejected"].includes(agent.status) || agent.detail === "Entry rejected before portfolio risk")
    ? `${agent.last_symbol}: ${detail}` : detail;
}
