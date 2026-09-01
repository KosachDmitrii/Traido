/** Alert when a BUY card becomes actionable (sound + helpers).
 *
 * Soft two-tone via Web Audio — no asset file. Browsers mute autoplay until
 * a gesture; `installBuyableAudioUnlock` arms the context on first click/key.
 */

import type { BuyOpportunity, DeskLight } from "@/lib/api";

let audioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (audioCtx) return audioCtx;
  const AC =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AC) return null;
  try {
    audioCtx = new AC();
  } catch {
    return null;
  }
  return audioCtx;
}

/** Resume AudioContext after the first user gesture (autoplay policy). */
export function installBuyableAudioUnlock(): () => void {
  const unlock = () => {
    const ctx = getAudioContext();
    if (ctx && ctx.state === "suspended") void ctx.resume();
  };
  window.addEventListener("pointerdown", unlock, { once: true });
  window.addEventListener("keydown", unlock, { once: true });
  return () => {
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
  };
}

/** Marimba / xylophone desk alert — wooden mallet, clear and noticeable.
 *
 * Short inharmonic partials + fast attack read as struck bars, not a phone
 * beep. Played twice so it still cuts through when the operator is away.
 */
export function playBuyableChime(): void {
  const ctx = getAudioContext();
  if (!ctx) return;
  void ctx.resume().then(() => {
    const now = ctx.currentTime;
    // C6 · E6 · G6 · C7 — bright xylophone climb
    const freqs = [1046.5, 1318.5, 1568.0, 2093.0];
    const noteSec = 0.42;
    const stepSec = 0.14;
    const peak = 0.26;

    const strike = (freq: number, t0: number) => {
      // Marimba-ish: strong fundamental, weak 3rd/6th partials, no sustained sine.
      for (const [mult, amp, type] of [
        [1, 1, "sine"],
        [3.01, 0.28, "triangle"],
        [6.0, 0.08, "sine"],
      ] as const) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = type;
        osc.frequency.value = freq * mult;
        const level = peak * amp;
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(level, t0 + 0.008);
        gain.gain.exponentialRampToValueAtTime(level * 0.25, t0 + 0.12);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + noteSec);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t0);
        osc.stop(t0 + noteSec + 0.02);
      }
    };

    for (let rep = 0; rep < 2; rep += 1) {
      const base = now + rep * (freqs.length * stepSec + 0.28);
      freqs.forEach((f, i) => strike(f, base + i * stepSec));
    }
  }).catch(() => undefined);
}

/** Force-resume audio (demo / after a synthetic click). */
export function unlockBuyableAudioNow(): void {
  const ctx = getAudioContext();
  if (ctx && ctx.state === "suspended") void ctx.resume();
}

/** Opportunity ids that are live-buyable right now (respects RTH gate). */
export function collectBuyable(
  light: DeskLight,
): Map<string, string> {
  const out = new Map<string, string>();
  if (light.session?.entries_allowed === false) return out;
  for (const opp of light.buy_opportunities ?? []) {
    if (isBuyable(opp)) {
      out.set(opp.id, opp.candidate.symbol);
    }
  }
  return out;
}

function isBuyable(opp: BuyOpportunity): boolean {
  return opp.viability?.state === "live" && opp.viability.buyable === true;
}
