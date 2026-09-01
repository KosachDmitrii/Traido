type Props = {
  name: string;
};

/** Brand lockup: T monogram + wordmark. */
export function BrandLogo({ name }: Props) {
  return (
    <div className="tb-brand" aria-label={name}>
      <svg
        className="tb-logo__mark"
        viewBox="0 0 28 28"
        width="28"
        height="28"
        aria-hidden
      >
        <rect width="28" height="28" rx="7" fill="currentColor" />
        <path
          fill="#ffffff"
          d="M7.25 8.1h13.5v3.05h-5.05V20.4h-3.4V11.15H7.25V8.1Z"
        />
      </svg>
      <span className="tb-logo__word">{name}</span>
    </div>
  );
}
