import type { BuyViability } from "@/lib/api";
import { useT } from "@/i18n/I18nProvider";

function price(value: unknown): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(3) : "—";
}

export function BuyZoneDetails({ viability }: { viability?: BuyViability }) {
  const t = useT();
  const values = viability?.measured;
  if (!values || values.ask == null) return null;
  return (
    <p className="opp-card__status mono">
      {t("opp.viability.zoneDetails", {
        ask: price(values.ask),
        low: price(values.allowed_zone_low),
        high: price(values.allowed_zone_high),
      })}
    </p>
  );
}
