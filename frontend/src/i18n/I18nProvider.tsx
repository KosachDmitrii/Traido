import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { t as translate, type MessageKey } from "./index";
import {
  getLocale,
  setLocale as setLocaleStore,
  subscribeLocale,
  type Locale,
  type Vars,
} from "./store";

type I18nValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, vars?: Vars) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => getLocale());

  useEffect(() => {
    document.documentElement.lang = getLocale();
    return subscribeLocale(() => setLocaleState(getLocale()));
  }, []);

  useEffect(() => {
    document.title = translate("meta.title");
  }, [locale]);

  const value: I18nValue = {
    locale,
    setLocale: setLocaleStore,
    t: translate,
  };

  return createElement(I18nContext.Provider, { value }, children);
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used within I18nProvider");
  return ctx;
}

/** Convenience: re-renders with locale and returns `t`. */
export function useT() {
  return useI18n().t;
}
