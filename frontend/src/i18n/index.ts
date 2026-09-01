import { en, type MessageKey } from "./en";
import { ru } from "./ru";
import { getLocale, interpolate, type Vars } from "./store";

const catalogs = { en, ru } as const;

export type { MessageKey } from "./en";
export type { Locale, Vars } from "./store";
export { getLocale, setLocale, subscribeLocale } from "./store";

export function t(key: MessageKey, vars?: Vars): string {
  const cat = catalogs[getLocale()] ?? en;
  const template = cat[key] ?? en[key] ?? String(key);
  return interpolate(template, vars);
}
