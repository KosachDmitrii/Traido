import { createPortal } from "react-dom";
import { useT } from "@/i18n/I18nProvider";
import type { FlashSlot, Toast } from "@/lib/toasts";

/** Bottom-right stack of desk messages.
 *
 * The layer is anchored to the bottom and laid out in reverse, so the array's
 * order — oldest first — is drawn bottom to top: a new message appears above
 * the ones already there and nothing that is being read moves. When the oldest
 * one goes it collapses the row it occupied, and the stack settles downward
 * into the gap instead of jumping.
 */
export function ToastStack({
  toasts,
  onDismiss,
  onHold,
}: {
  toasts: Toast[];
  onDismiss: (id: FlashSlot) => void;
  onHold: (id: FlashSlot, held: boolean) => void;
}) {
  const t = useT();

  if (toasts.length === 0) return null;

  return createPortal(
    <div className="toast-layer">
      {toasts.map(({ id, message, leaving }) => (
        <div key={id} className={`toast-slot${leaving ? " toast-slot--leaving" : ""}`}>
          {/* Carries no padding of its own, which is the whole reason it is
              here: the row can only collapse as far as its own box, and the
              card's padding would floor it 30px short. */}
          <div className="toast-slot__inner">
            <div
              className={`flash flash--${message.kind} toast${leaving ? " toast--leaving" : ""}`}
              role={message.kind === "error" ? "alert" : "status"}
              aria-live={message.kind === "error" ? "assertive" : "polite"}
              onMouseEnter={() => onHold(id, true)}
              onMouseLeave={() => onHold(id, false)}
            >
              <div className="flash__body">
                <strong className="flash__title">{message.title}</strong>
                {message.detail ? <p className="flash__detail">{message.detail}</p> : null}
              </div>
              {/* A pending toast has no close button and no countdown: it is
                  the only sign that a broker call is still in flight. */}
              {message.kind !== "pending" ? (
                <button
                  type="button"
                  className="flash__close"
                  onClick={() => onDismiss(id)}
                  aria-label={t("toast.dismiss")}
                >
                  ×
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ))}
    </div>,
    document.body,
  );
}
