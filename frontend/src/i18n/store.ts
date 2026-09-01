/** Lightweight i18n — no external dependency. */

export type Locale = "en" | "ru";

const STORAGE_KEY = "TRAIDO_LOCALE";

type Listener = () => void;

let locale: Locale = readInitial();
const listeners = new Set<Listener>();

function readInitial(): Locale {
  if (typeof window === "undefined") return "en";
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw === "ru" || raw === "en" ? raw : "en";
}

export function getLocale(): Locale {
  return locale;
}

export function setLocale(next: Locale): void {
  if (next === locale) return;
  locale = next;
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, next);
    document.documentElement.lang = next;
  }
  listeners.forEach((l) => l());
}

export function subscribeLocale(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export type Vars = Record<string, string | number | null | undefined>;

/** Replace `{name}` placeholders. Missing vars stay as the placeholder. */
export function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, key: string) => {
    const v = vars[key];
    return v == null ? `{${key}}` : String(v);
  });
}
