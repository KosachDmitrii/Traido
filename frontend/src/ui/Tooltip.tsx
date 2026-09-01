import { Tooltip } from "@base-ui/react/tooltip";
import type { ReactNode } from "react";
import styles from "./Tooltip.module.css";

export function TooltipProvider({ children }: { children: ReactNode }) {
  return <Tooltip.Provider delay={220}>{children}</Tooltip.Provider>;
}

type Props = {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
};

export function HintTooltip({ content, children, side = "top" }: Props) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger render={<span className={styles.Trigger} />}>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side={side} sideOffset={8} className={styles.Positioner}>
          <Tooltip.Popup className={styles.Popup}>{content}</Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
