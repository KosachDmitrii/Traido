/** The stack of transient desk messages.
 *
 * One message at a time was wrong for the thing being reported. Confirmations
 * arrive in bursts — approve two cards and the second "отправляем BUY…" erased
 * the first one's result before anyone read it, so the desk silently lost the
 * answer to a question about capital. A stack keeps every answer until it has
 * had its five seconds.
 *
 * The one thing that must *not* stack is a `pending` message and the result
 * that resolves it: those are two states of one request, not two events. That
 * is what `replacing` is for — the caller holds the slot its pending message
 * went into and hands the result back to the same row.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { FlashMessage } from "@/lib/messages";

/** A row in the stack. Opaque; only useful to hand back to `show`. */
export type FlashSlot = number;

export type Toast = {
  id: FlashSlot;
  message: FlashMessage;
  /** True while the exit animation runs. The row is still in the DOM, and the
   *  rows above it are sliding down into the space it is giving up. */
  leaving: boolean;
};

/** Long enough to read two lines, short enough that a burst does not queue. */
const LIFE_MS = 5000;

/** Must match the transition in `.toast-slot` — the row is dropped when the
 *  collapse it animates has finished. */
export const EXIT_MS = 200;

/** Past this the stack stops being a notification and becomes a wall. */
const MAX_VISIBLE = 4;

/** How long a request may go unanswered before the desk stops claiming to be
 *  waiting for it. Comfortably past the ~45s a fill can legitimately take. */
const STALL_MS = 90_000;

/** What a pending message becomes when nothing ever answers it.
 *
 * Not a dismissal. `fetch` has no timeout, so a request that is accepted and
 * then abandoned — a backend restarting under it is enough — leaves a promise
 * that never settles and a spinner that never resolves. Silently clearing that
 * would say the request is over; it is not, and for a BUY the outcome is
 * genuinely unknown. So the row says so, and waits to be acknowledged. */
const STALLED_DETAIL =
  "Ответ не пришёл за 90 с. Что с запросом стало — неизвестно. " +
  "Проверь Positions и Activity, прежде чем повторять.";

export type ToastQueue = {
  /** Oldest first. The layer renders bottom-up, so this is bottom to top. */
  toasts: Toast[];
  /** Shows a message and returns its slot. Pass a slot from an earlier call to
   *  replace that row in place instead of adding one. */
  show: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  dismiss: (id: FlashSlot) => void;
  /** Suspends one row's countdown, e.g. while the pointer rests on it. */
  hold: (id: FlashSlot, held: boolean) => void;
};

export function useToastQueue(): ToastQueue {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef<FlashSlot>(1);
  // Slots that may still be written to: a row that is leaving must not be
  // revived by a late result, and a slot nobody holds any more must not be
  // reused by the counter.
  const open = useRef(new Set<FlashSlot>());
  const timers = useRef(new Map<FlashSlot, number>());
  const listRef = useRef<Toast[]>([]);

  useEffect(() => {
    listRef.current = toasts;
  }, [toasts]);

  const disarm = useCallback((id: FlashSlot) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) window.clearTimeout(timer);
    timers.current.delete(id);
  }, []);

  const drop = useCallback((id: FlashSlot) => {
    timers.current.delete(id);
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const dismiss = useCallback(
    (id: FlashSlot) => {
      if (!open.current.has(id)) return;
      open.current.delete(id);
      disarm(id);
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
      timers.current.set(id, window.setTimeout(() => drop(id), EXIT_MS));
    },
    [disarm, drop],
  );

  const stall = useCallback(
    (id: FlashSlot) => {
      disarm(id);
      // The slot stays open: an answer that arrives late still belongs in this
      // row, and replacing the stall notice with it is the right outcome.
      setToasts((prev) =>
        prev.map((t) =>
          t.id === id && t.message.kind === "pending"
            ? {
                ...t,
                message: {
                  kind: "error",
                  title: t.message.title,
                  detail: STALLED_DETAIL,
                  sticky: true,
                },
              }
            : t,
        ),
      );
    },
    [disarm],
  );

  const arm = useCallback(
    (id: FlashSlot, message: FlashMessage) => {
      disarm(id);
      // Waits for a click rather than a clock: it is reporting something the
      // operator has to act on, and five seconds is not an acknowledgement.
      if (message.sticky) return;
      // A pending message is not a report but the only sign that a broker call
      // is in flight, so it is not on the ordinary countdown — it goes when its
      // result replaces it. It still gets an outer bound, because a request
      // that never comes back must not leave the desk waiting for ever.
      if (message.kind === "pending") {
        timers.current.set(id, window.setTimeout(() => stall(id), STALL_MS));
        return;
      }
      timers.current.set(id, window.setTimeout(() => dismiss(id), LIFE_MS));
    },
    [disarm, dismiss, stall],
  );

  const show = useCallback(
    (message: FlashMessage, replacing?: FlashSlot): FlashSlot => {
      // A slot that has already gone — dismissed by hand, or timed out while
      // the broker was slow — is not resurrected. The result gets a new row.
      const id =
        replacing !== undefined && open.current.has(replacing) ? replacing : nextId.current++;
      open.current.add(id);

      setToasts((prev) => {
        const at = prev.findIndex((t) => t.id === id);
        if (at >= 0) {
          const next = prev.slice();
          next[at] = { ...next[at], message, leaving: false };
          return next;
        }
        const next = [...prev, { id, message, leaving: false }];
        // Over the cap the oldest is cut rather than animated out: the cap is
        // only reached by a burst, and the row being cut has been on screen
        // longest. Its timer is left to fire into an empty list.
        return next.length > MAX_VISIBLE ? next.slice(next.length - MAX_VISIBLE) : next;
      });

      arm(id, message);
      return id;
    },
    [arm],
  );

  const hold = useCallback(
    (id: FlashSlot, held: boolean) => {
      if (!open.current.has(id)) return;
      if (held) {
        disarm(id);
        return;
      }
      // Restarts the countdown rather than resuming what was left of it: the
      // pointer was on the message, so it is being read now, not five seconds ago.
      const row = listRef.current.find((t) => t.id === id);
      if (row) arm(id, row.message);
    },
    [arm, disarm],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((timer) => window.clearTimeout(timer));
      pending.clear();
    };
  }, []);

  return { toasts, show, dismiss, hold };
}
