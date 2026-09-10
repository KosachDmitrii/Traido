import { Button as BaseButton } from "@base-ui/react/button";
import type { ComponentProps, ReactNode } from "react";
import { LoadingDots } from "./LoadingDots";
import { useT } from "@/i18n/I18nProvider";
import styles from "./Button.module.css";

type Variant = "accent" | "ink" | "ghost" | "light" | "link";

type Props = Omit<ComponentProps<typeof BaseButton>, "className"> & {
  loading?: boolean;
  variant?: Variant;
  className?: string;
  children: ReactNode;
};

const VARIANT: Partial<Record<Variant, string | undefined>> = {
  accent: styles.accent,
  ink: styles.ink,
  ghost: styles.ghost,
  light: styles.light,
  link: styles.link,
};

export function Button({ variant = "accent", className = "", children, loading = false, ...rest }: Props) {
  const t = useT();
  return (
    <BaseButton
      className={[styles.Button, VARIANT[variant], className].filter(Boolean).join(" ")}
      {...rest}
      disabled={loading || rest.disabled}
      aria-busy={loading || rest["aria-busy"]}
    >
      {loading && <LoadingDots ariaLabel={t("common.loading")} />}
      {children}
    </BaseButton>
  );
}
