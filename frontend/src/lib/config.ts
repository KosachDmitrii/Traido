/**
 * Runtime API configuration.
 *
 * In development the Vite dev server proxies `/api` to the backend, so a
 * relative path is correct and the base is empty. A deployed build is served
 * by nginx with no proxy, so it needs an absolute base — supplied at build
 * time via `VITE_API_BASE_URL`.
 *
 * Vite inlines `import.meta.env.VITE_*` at compile time, so this must be baked
 * into the image build rather than injected when the container starts.
 */

const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim();

/** Base URL with no trailing slash. Empty string means "same origin". */
export const API_BASE = RAW_BASE.replace(/\/+$/, "");

/** Build a full URL for an API path. Pass paths with a leading slash. */
export function apiUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${suffix}`;
}

const API_KEY_STORAGE_KEY = "TRAIDO_API_KEY";

export function getStoredApiKey(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(API_KEY_STORAGE_KEY);
}

/**
 * EventSource cannot send headers, so the API key rides as a query parameter
 * for the SSE stream only. Everything else uses the X-API-Key header.
 */
export function streamUrl(path: string): string {
  const key = getStoredApiKey();
  const url = apiUrl(path);
  if (!key) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}api_key=${encodeURIComponent(key)}`;
}
