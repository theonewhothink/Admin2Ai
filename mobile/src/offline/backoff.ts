/**
 * Retry timing for uploads (§43). Exponential with "equal jitter": half the
 * delay is fixed, half is random, so retries from many phones spread out while
 * each phone still waits at least half the nominal delay.
 */

export interface BackoffPolicy {
  /** Delay after the first failed attempt. */
  baseMs: number;
  /** Upper bound for any single delay. */
  maxMs: number;
  factor: number;
  /** Share of the delay that is randomised, 0..1. */
  jitter: number;
}

export const DEFAULT_BACKOFF: BackoffPolicy = {
  baseMs: 5_000,
  maxMs: 30 * 60_000,
  factor: 2,
  jitter: 0.5,
};

/** Longest server-requested wait we honour (Retry-After), to keep the queue moving. */
export const MAX_RETRY_AFTER_MS = 60 * 60_000;

/**
 * Delay before retrying after `attempt` failed attempts (attempt >= 1).
 * `random` must return a number in [0, 1); inject a fixed one in tests.
 */
export function backoffDelay(attempt: number, policy: BackoffPolicy, random: () => number): number {
  const n = Math.max(1, Math.floor(attempt));
  const nominal = Math.min(policy.maxMs, policy.baseMs * Math.pow(policy.factor, n - 1));
  const jitter = Math.min(1, Math.max(0, policy.jitter));
  const r = Math.min(Math.max(random(), 0), 1);
  return Math.round(nominal * (1 - jitter) + nominal * jitter * r);
}

/** Final delay: our backoff, but never sooner than the server asked (bounded). */
export function retryDelay(
  attempt: number,
  policy: BackoffPolicy,
  random: () => number,
  retryAfterMs?: number,
): number {
  const ours = backoffDelay(attempt, policy, random);
  if (retryAfterMs === undefined || !Number.isFinite(retryAfterMs) || retryAfterMs <= 0) return ours;
  return Math.max(ours, Math.min(retryAfterMs, MAX_RETRY_AFTER_MS));
}

/**
 * Parse an HTTP Retry-After header (delta-seconds or HTTP-date) into ms.
 * Returns undefined when absent or unreadable.
 */
export function parseRetryAfter(value: string | null | undefined, nowMs: number): number | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (/^\d+$/.test(trimmed)) return Number(trimmed) * 1000;
  const at = Date.parse(trimmed);
  if (Number.isNaN(at)) return undefined;
  return Math.max(0, at - nowMs);
}
