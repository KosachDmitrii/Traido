import { Select } from "@base-ui/react/select";
import { Check, ChevronDown } from "lucide-react";
import styles from "./SelectField.module.css";

export type SelectOption = { value: string; label: string };

type Props = {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
};

export function SelectField({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
  className = "",
}: Props) {
  return (
    <Select.Root
      value={value}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next);
      }}
      items={options}
    >
      <Select.Trigger
        className={[styles.Select, className].filter(Boolean).join(" ")}
        aria-label={ariaLabel}
      >
        <Select.Value placeholder={placeholder} />
        <Select.Icon className={styles.Icon}>
          <ChevronDown size={14} strokeWidth={2} absoluteStrokeWidth aria-hidden />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Positioner
          className={styles.Positioner}
          side="bottom"
          align="start"
          sideOffset={6}
          alignItemWithTrigger={false}
        >
          <Select.Popup className={styles.Popup}>
            <Select.List className={styles.List}>
              {options.map((opt) => (
                <Select.Item key={opt.value} value={opt.value} className={styles.Item}>
                  <span className={styles.IndicatorSlot} aria-hidden>
                    <Select.ItemIndicator className={styles.Indicator}>
                      <Check size={14} strokeWidth={2.25} absoluteStrokeWidth />
                    </Select.ItemIndicator>
                  </span>
                  <Select.ItemText className={styles.ItemText}>{opt.label}</Select.ItemText>
                </Select.Item>
              ))}
            </Select.List>
          </Select.Popup>
        </Select.Positioner>
      </Select.Portal>
    </Select.Root>
  );
}
