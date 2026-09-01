import { Button as BaseButton } from "@base-ui/react/button";
import type { ComponentProps, ReactNode } from "react";
import styles from "./Button.module.css";

type Variant = "accent" | "ink" | "ghost" | "light" | "link";

type Props = Omit<ComponentProps<typeof BaseButton>, "className"> & {
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

export function Button({ variant = "accent", className = "", children, ...rest }: Props) {
  return (
    <BaseButton
      className={[styles.Button, VARIANT[variant], className].filter(Boolean).join(" ")}
      {...rest}
    >
      {children}
    </BaseButton>
  );
}
