/** Shared desk polling state for all dashboard pages. */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  type BrokerSnapshot,
  type DeskLight,
  type DeskResponse,
  fetchBroker,
  fetchDeskLight,
  fetchKillSwitch,
  mergeDesk,
  subscribeDeskEvents,
} from "@/lib/api";
import { humanizeError, type FlashMessage } from "@/lib/messages";
import { useToastQueue, type FlashSlot, type Toast } from "@/lib/toasts";

const LIGHT_MS = 5000;
const LIGHT_FAST_MS = 1500;

/** How often the broker snapshot is re-fetched.
 *
 * Exported because the Positions panel states this rate to the operator, and a
 * subtitle that names an interval has to be reading the interval. It said
 * "(15–20s)" while both numbers behind it were about to change.
 */
export const BROKER_MS = 10000;

/** Four states, not a boolean.
 *
 * "not read yet" and "could not be read" are different facts and only one of
 * them is worth alarming about, while neither may be drawn as "off" — that
 * would tell the operator trading is armed on the strength of a request that
 * never came back. */
export type KillSwitchState = "loading" | "on" | "off" | "unreadable";

type DeskContextValue = {
  desk: DeskResponse | null;
  scannerLine: string;
  killSwitch: KillSwitchState;
  refreshKillSwitch: () => Promise<void>;
  toasts: Toast[];
  /** Shows a message. Pass the slot returned by an earlier call to replace that
   *  message in place — how a `pending` toast becomes its own result rather
   *  than leaving "отправляем BUY…" stacked next to the answer. */
  showFlash: (message: FlashMessage, replacing?: FlashSlot) => FlashSlot;
  dismissFlash: (id: FlashSlot) => void;
  /** Suspends one message's countdown, e.g. while the pointer rests on it. */
  holdFlash: (id: FlashSlot, held: boolean) => void;
  refreshAll: () => Promise<void>;
  refreshLight: () => Promise<void>;
};

const DeskContext = createContext<DeskContextValue | null>(null);

export function DeskProvider({ children }: { children: ReactNode }) {
  const [light, setLight] = useState<DeskLight | null>(null);
  const [broker, setBroker] = useState<BrokerSnapshot | null>(null);
  const [scannerLine, setScannerLine] = useState("Starting…");
  const [killSwitch, setKillSwitch] = useState<KillSwitchState>("loading");
  const lightRef = useRef<DeskLight | null>(null);
  const lightInFlight = useRef(false);
  const lightQueued = useRef(false);
  const brokerBusy = useRef(false);

  // The queue lives here, above the toast layer, so that a message raised on a
  // page that draws no toasts still expires there. Owning the countdown in the
  // visible component would let a confirmation from /settings sit in state and
  // surface minutes later, the next time the dashboard is opened.
  const {
    toasts,
    show: showFlash,
    dismiss: dismissFlash,
    hold: holdFlash,
  } = useToastQueue();

  const applyLight = useCallback((next: DeskLight) => {
    lightRef.current = next;
    setLight(next);
    const s = next.scanner || {};
    setScannerLine(
      s.running
        ? `Scanning ${s.last_symbol || "…"} · cycle ${s.cycle}`
        : `Watching ${(s.universe || []).length} symbols · cycle ${s.cycle || 0} · buys ${next.buy_opportunities.length} · sells ${next.sell_opportunities.length}`,
    );
  }, []);

  const refreshLight = useCallback(async () => {
    if (lightInFlight.current) {
      lightQueued.current = true;
      return;
    }
    lightInFlight.current = true;
    try {
      do {
        lightQueued.current = false;
        const next = await fetchDeskLight();
        if (next) applyLight(next);
      } while (lightQueued.current);
    } finally {
      lightInFlight.current = false;
    }
  }, [applyLight]);

  const refreshBroker = useCallback(async (fresh = false) => {
    if (brokerBusy.current) return;
    brokerBusy.current = true;
    try {
      setBroker(await fetchBroker(fresh));
    } finally {
      brokerBusy.current = false;
    }
  }, []);

  // Lives here rather than on the settings page because it is the one switch
  // that makes every BUY on the desk fail, and learning that from the failure
  // is learning it too late.
  const refreshKillSwitch = useCallback(async () => {
    try {
      setKillSwitch((await fetchKillSwitch()).enabled ? "on" : "off");
    } catch {
      setKillSwitch("unreadable");
    }
  }, []);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshLight(), refreshBroker(true), refreshKillSwitch()]);
  }, [refreshLight, refreshBroker, refreshKillSwitch]);

  useEffect(() => {
    let alive = true;
    let lightTimer: ReturnType<typeof setTimeout> | null = null;

    const scheduleLight = () => {
      if (!alive) return;
      const running = Boolean(lightRef.current?.scanner?.running);
      lightTimer = setTimeout(() => {
        refreshLight()
          .catch(() => undefined)
          .finally(() => {
            if (alive) scheduleLight();
          });
      }, running ? LIGHT_FAST_MS : LIGHT_MS);
    };

    // Deliberately outside the sequence below: it is a cheap local read, and
    // chaining it after the broker call would leave the header unable to say
    // whether trading is blocked for as long as the broker is slow — or for
    // good, if the broker call throws.
    refreshKillSwitch().catch(() => undefined);

    // No scan is requested here. The loop is started by the API's lifespan hook
    // and paced by `scan_interval_seconds`; asking for a pass on every mount let
    // page loads drive the scan cadence, and in development the double-invoked
    // effect asked twice per load.
    (async () => {
      try {
        if (!alive) return;
        await refreshLight();
        await refreshBroker(false);
      } catch (err) {
        if (alive) {
          showFlash(humanizeError(err instanceof Error ? err.message : String(err)));
          setScannerLine("Dashboard unavailable");
        }
      } finally {
        if (alive) scheduleLight();
      }
    })();

    const brokerId = setInterval(() => {
      refreshBroker(false).catch(() => undefined);
      refreshKillSwitch().catch(() => undefined);
    }, BROKER_MS);

    const unsub = subscribeDeskEvents((ev) => {
      if (ev.channel === "broker" || ev.type === "decide" || ev.type === "decide_failed") {
        refreshBroker(true).catch(() => undefined);
      }
      if (
        ev.type === "opportunity" ||
        ev.type === "decide" ||
        ev.type === "decide_failed" ||
        ev.type === "scan_cycle" ||
        ev.type === "exit_decided" ||
        ev.type === "exit_failed"
      ) {
        refreshLight().catch(() => undefined);
      }
    });

    return () => {
      alive = false;
      if (lightTimer) clearTimeout(lightTimer);
      clearInterval(brokerId);
      unsub();
    };
  }, [refreshLight, refreshBroker, refreshKillSwitch, showFlash]);

  const desk = useMemo(
    () => (light ? mergeDesk(light, broker) : null),
    [light, broker],
  );

  const value = useMemo(
    () => ({
      desk,
      scannerLine,
      killSwitch,
      refreshKillSwitch,
      toasts,
      showFlash,
      dismissFlash,
      holdFlash,
      refreshAll,
      refreshLight,
    }),
    [
      desk,
      scannerLine,
      killSwitch,
      refreshKillSwitch,
      toasts,
      showFlash,
      dismissFlash,
      holdFlash,
      refreshAll,
      refreshLight,
    ],
  );

  return <DeskContext.Provider value={value}>{children}</DeskContext.Provider>;
}

export function useDesk() {
  const ctx = useContext(DeskContext);
  if (!ctx) throw new Error("useDesk must be used within DeskProvider");
  return ctx;
}
