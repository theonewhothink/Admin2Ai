/**
 * Where the owner is in onboarding (production), kept in sessionStorage so
 * the flow resumes at the right step after the round trip to Google,
 * Microsoft or the bank. Nothing sensitive is stored: a step number and which
 * connection the owner left to make.
 */

const KEY = "admin2ai:onboarding";
/** A return later than this is not the end of that round trip. */
export const RETURN_WINDOW_MS = 30 * 60 * 1000;

export type Leg = "email" | "bank";

export interface Progress {
  step: number;
  left?: Leg;
  leftAt?: number;
}

export function readProgress(): Progress | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    const v: unknown = raw ? JSON.parse(raw) : null;
    if (!v || typeof v !== "object" || typeof (v as Progress).step !== "number") return null;
    const p = v as Progress;
    return {
      step: Math.max(0, Math.floor(p.step)),
      ...(p.left === "email" || p.left === "bank" ? { left: p.left } : {}),
      ...(typeof p.leftAt === "number" ? { leftAt: p.leftAt } : {}),
    };
  } catch {
    return null;
  }
}

export function saveProgress(p: Progress) {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    // Storage unavailable: the flow still works, it just starts at the top after a reload.
  }
}

export function clearProgress() {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

/** The step to show, as a primitive for useSyncExternalStore (-1: nothing saved or not in a browser). */
export function savedStep(): number {
  return readProgress()?.step ?? -1;
}

/**
 * The owner has just come back from a provider's consent page this flow sent
 * them to. `search` is the landing page's query string.
 * Returns what happened, or null when this is not such a return.
 */
export function returnFrom(p: Progress | null, search: string, now: number): { leg: Leg; result: "done" | "failed" | "back" } | null {
  if (!p?.left || typeof p.leftAt !== "number" || now - p.leftAt > RETURN_WINDOW_MS || now < p.leftAt) return null;
  const q = new URLSearchParams(search);
  // Google / Microsoft: the API sends the owner back with ?signin=done|failed.
  const signin = q.get("signin");
  if (signin) return { leg: p.left, result: signin === "done" ? "done" : "failed" };
  // Open Banking: the bank sends the owner back with ?ref=… (and ?error=… when refused).
  if (q.get("error")) return { leg: p.left, result: "failed" };
  if (q.get("ref") || q.get("bank") || q.get("connected")) return { leg: p.left, result: "back" };
  return null;
}
