import { Switch } from "@base-ui/react/switch";
import styles from "./SwitchControl.module.css";

type Props = {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  "aria-label"?: string;
};

export function SwitchControl({ checked, onCheckedChange, disabled, ...rest }: Props) {
  return (
    <Switch.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      className={styles.Switch}
      {...rest}
    >
      <Switch.Thumb className={styles.Thumb} />
    </Switch.Root>
  );
}
