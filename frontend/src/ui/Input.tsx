import { Input as BaseInput } from "@base-ui/react/input";
import type { ComponentProps } from "react";
import styles from "./Input.module.css";

type Props = ComponentProps<typeof BaseInput> & { className?: string };

export function Input({ className = "", ...rest }: Props) {
  return <BaseInput className={[styles.Input, className].filter(Boolean).join(" ")} {...rest} />;
}
