import styles from "./LoadingDots.module.css";

type LoadingDotsProps = {
  className?: string;
  /** Defaults to "Loading". Pass a translated string from the caller. */
  ariaLabel?: string;
};

export function LoadingDots({ className = "", ariaLabel = "Loading" }: LoadingDotsProps) {
  return (
    <span
      className={`${styles.loader} ${className}`.trim()}
      role="status"
      aria-label={ariaLabel}
    >
      {Array.from({ length: 9 }).map((_, index) => (
        <span key={index} className={styles.dot} />
      ))}
    </span>
  );
}
