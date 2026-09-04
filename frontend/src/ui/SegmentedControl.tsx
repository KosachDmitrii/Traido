import { Toggle } from "@base-ui/react/toggle";
import { ToggleGroup } from "@base-ui/react/toggle-group";
import type { ReactNode } from "react";
import styles from "./SegmentedControl.module.css";

export type SegmentOption = { value: string; label: ReactNode };

type Props = {
  value: string;
  onChange: (value: string) => void;
  options: SegmentOption[];
  ariaLabel: string;
  className?: string;
  wide?: boolean;
};

export function SegmentedControl({
  value,
  onChange,
  options,
  ariaLabel,
  className = "",
  wide = false,
}: Props) {
  return (
    <ToggleGroup
      value={[value]}
      onValueChange={(next) => {
        // Base UI can clear the group (empty array) on a second click — keep the
        // current step so «Слабо» cannot silently fall back after a remount.
        const picked = next[0] ?? value;
        if (picked && picked !== value) onChange(picked);
      }}
      aria-label={ariaLabel}
      className={[styles.Panel, wide ? styles.PanelWide : "", className].filter(Boolean).join(" ")}
    >
      {options.map((opt) => (
        <Toggle key={opt.value} value={opt.value} className={styles.Button}>
          {opt.label}
        </Toggle>
      ))}
    </ToggleGroup>
  );
}
