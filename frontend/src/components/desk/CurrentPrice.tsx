import { useState } from "react";
import { ArrowUp, ArrowDown } from "lucide-react";
import { px } from "./orbLabels";
import styles from "./OpportunityRail.module.css";

type Tick = { price: number | null; direction: "up" | "down" | "flat" };

export function CurrentPrice({ value }: { value: unknown }) {
  const parsed = value == null || value === "" ? NaN : Number(value);
  const price = Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  const [tick, setTick] = useState<Tick>({ price, direction: "flat" });
  // Reset on missing data. Equal quotes retain the last observed movement.
  if (price !== tick.price) {
    setTick({ price, direction: price == null || tick.price == null ? "flat" : price > tick.price ? "up" : "down" });
  }
  const direction = tick.direction;
  const label = direction === "up" ? "рост" : direction === "down" ? "снижение" : "без изменения";
  return <span className={styles.currentPrice} data-direction={direction}
    title={direction === "flat" ? "Текущая цена" : "По сравнению с предыдущей полученной котировкой"}
    aria-label={price == null ? "Цена недоступна" : `${px(price)}, ${label}`}>
    {px(price)}
    {direction === "up" && <ArrowUp size={16} strokeWidth={2.5} aria-hidden="true" />}
    {direction === "down" && <ArrowDown size={16} strokeWidth={2.5} aria-hidden="true" />}
  </span>;
}
